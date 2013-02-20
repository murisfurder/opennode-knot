from grokcore.component import context, subscribe, baseclass
from logging import DEBUG
from twisted.internet import defer
from twisted.python import log
from uuid import uuid5, NAMESPACE_DNS
from zope.component import handle
import netaddr

from opennode.knot.backend.v12ncontainer import (IVirtualizationContainerSubmitter,
                                                 backends,
                                                 SyncVmsAction)
from opennode.knot.backend.operation import (IGetVirtualizationContainers,
                                             IStartVM,
                                             IShutdownVM,
                                             IDestroyVM,
                                             ISuspendVM,
                                             IResumeVM,
                                             IListVMS,
                                             IRebootVM,
                                             IGetComputeInfo,
                                             IDeployVM,
                                             IUndeployVM,
                                             IGetLocalTemplates,
                                             IGetDiskUsage,
                                             IGetHWUptime,
                                             IGetRoutes,
                                             IUpdateVM,
                                             IMigrateVM,
                                             OperationRemoteError)
from opennode.knot.model.compute import ICompute, Compute, IVirtualCompute
from opennode.knot.model.compute import IUndeployed, IDeployed, IDeploying
from opennode.knot.model.compute import IManageable
from opennode.knot.model.console import TtyConsole, SshConsole, OpenVzConsole, VncConsole
from opennode.knot.model.network import NetworkInterface, NetworkRoute
from opennode.knot.model.template import Template
from opennode.knot.model.virtualizationcontainer import IVirtualizationContainer, VirtualizationContainer

from opennode.oms.config import get_config
from opennode.oms.endpoint.ssh.detached import DetachedProtocol
from opennode.oms.endpoint.ssh.cmdline import VirtualConsoleArgumentParser
from opennode.oms.model.form import (IModelModifiedEvent, IModelDeletedEvent, IModelCreatedEvent,
                                     ModelModifiedEvent, TmpObj, alsoProvides, noLongerProvides)
from opennode.oms.model.model.actions import Action, action
from opennode.oms.model.model.symlink import Symlink, follow_symlinks
from opennode.oms.util import blocking_yield, get_u, get_i, get_f, exception_logger
from opennode.oms.zodb import db


def any_stack_installed(context):
    return IManageable.providedBy(context)


def format_error(e):
    return (": ".join(msg for msg in e.args if isinstance(msg, str) and not msg.startswith('  File "/')))


@defer.inlineCallbacks
def register_machine(host, mgt_stack=None):

    @db.ro_transact
    def check():
        machines = db.get_root()['oms_root']['machines']
        machine = follow_symlinks(machines['by-name'][host])
        if not mgt_stack.providedBy(machine):
            return
        return machine

    @db.transact
    def update():
        machines = db.get_root()['oms_root']['machines']
        machine = Compute(unicode(host), u'active', mgt_stack=mgt_stack)
        machine.__name__ = str(uuid5(NAMESPACE_DNS, host))
        machines.add(machine)

    if not (yield check()):
        yield update()


def find_compute_v12n_container(compute, backend):
    for v12nc in compute:
        if IVirtualizationContainer.providedBy(v12nc) and v12nc.backend == backend:
            return v12nc


class ComputeAction(Action):
    context(ICompute)
    baseclass()

    _lock_registry = {}

    @db.ro_transact(proxy=False)
    def subject(self, *args, **kwargs):
        return tuple((self.context.__parent__.__parent__,))

    def locked(self):
        return self.context in self._lock_registry

    def lock(self):
        """ with-statement support method """
        self._lock_registry[self.context] = defer.Deferred()

    def unlock(self, d):
        """ with-statement support method """
        d.chainDeferred(self._lock_registry[self.context])

    def execute(self, cmd, args):
        if self.locked():
            self._lock_registry[self.context].addBoth(lambda r: self.execute(cmd, args))
            return
        self.lock()
        d = self._execute(cmd, args)
        self.unlock(d)
        return d

    def _execute(self, cmd, args):
        """ Must be overloaded by child classes """
        raise NotImplementedError()


class VComputeAction(ComputeAction):
    """Common code for virtual compute actions."""
    context(IVirtualCompute)
    baseclass()

    @defer.inlineCallbacks
    def _execute(self, cmd, args):
        action_name = getattr(self, 'action_name', self._name + "ing")

        name = yield db.get(self.context, '__name__')
        parent = yield db.get(self.context, '__parent__')

        cmd.write("%s %s\n" % (action_name, name))
        submitter = IVirtualizationContainerSubmitter(parent)

        try:
            yield submitter.submit(self.job, name)
        except Exception as e:
            cmd.write("%s\n" % format_error(e))



class AllocateAction(ComputeAction):
    context(IUndeployed)
    action('allocate')

    @defer.inlineCallbacks
    def _execute(self, cmd, args):

        @db.ro_transact
        def get_matching_machines(container):
            all_machines = db.get_root()['oms_root']['machines']
            param = unicode(get_config().getstring('allocate', 'diskspace_filter_param',
                                                   default=u'/storage'))

            if param not in self.context.diskspace:
                raise KeyError(param)

            log.msg('Searching in: %s' % (
                map(lambda m: (m, (self.context.memory_usage < getattr(m, 'memory', None),
                                   self.context.diskspace[param] < getattr(m, 'diskspace', {}).get(param, 0),
                                   self.context.num_cores <= getattr(m, 'num_cores', None))), all_machines)),
                logLevel=DEBUG, system='action-allocate')

            return filter(lambda m: (ICompute.providedBy(m) and
                                     find_compute_v12n_container(m, container) and
                                     self.context.memory_usage < m.memory and
                                     self.context.diskspace[param] < m.diskspace.get(param, 0) and
                                     self.context.num_cores <= m.num_cores), all_machines)

        vmsbackend = yield db.ro_transact(lambda: self.context.__parent__.backend)()
        machines = yield get_matching_machines(vmsbackend)

        if len(machines) <= 0:
            log.msg('Found no fitting machines to allocate to. Action aborted.', system='action-allocate')
            cmd.write('Found no fitting machines to allocate to. Aborting.\n')
            return

        @db.ro_transact
        def rank(machines):
            return sorted(machines, key=lambda m: m.__name__)

        best = (yield rank(machines))[0]

        log.msg('Found %s as the best candidate. Attempting to allocate...' % (best),
                system='action-allocate')

        bestvmscontainer = yield db.ro_transact(find_compute_v12n_container)(best, vmsbackend)

        yield DeployAction(self.context).execute(DetachedProtocol(), bestvmscontainer)


class DeployAction(VComputeAction):
    context(IUndeployed)

    action('deploy')

    @defer.inlineCallbacks
    def execute(self, cmd, args):
        try:
            self.context.lock()
            yield self._execute(cmd, args)
        finally:
            self.context.unlock()

    @defer.inlineCallbacks
    def _execute(self, cmd, args):
        template = yield db.get(self.context, 'template')

        if not template:
            cmd.write("Cannot deploy %s because no template was specified\n" % self.context.hostname)
            return

        @db.ro_transact(proxy=False)
        def get_parameters():
            return {'template_name': self.context.template,
                    'hostname': self.context.hostname,
                    'vm_type': self.context.__parent__.backend,
                    'uuid': self.context.__name__,
                    'nameservers': db.remove_persistent_proxy(self.context.nameservers),
                    'autostart': self.context.autostart,
                    'ip_address': self.context.ipv4_address.split('/')[0],
                    'passwd': getattr(self.context, 'root_password', None)}

        @db.transact
        def cleanup_root_password():
            if getattr(self.context, 'root_password', None) is not None:
                self.context.root_password = None

        target = (args if IVirtualizationContainer.providedBy(args)
                  else (yield db.get(self.context, '__parent__')))

        try:
            yield db.transact(alsoProvides)(self.context, IDeploying)
            vm_parameters = yield get_parameters()
            res = yield IVirtualizationContainerSubmitter(target).submit(IDeployVM, vm_parameters)
            yield cleanup_root_password()
            log.msg('IDeployVM result: %s' % res, system='action-deploy')

            @db.transact
            def finalize_vm():
                noLongerProvides(self.context, IDeploying)
                noLongerProvides(self.context, IUndeployed)
                alsoProvides(self.context, IDeployed)
                log.msg('Deployment of "%s" is finished' % (vm_parameters['hostname']), system='deploy')
                cmd.write("Changed state from undeployed to deployed\n")

            yield finalize_vm()
        finally:
            @db.transact
            def cleanup_deploying():
                noLongerProvides(self.context, IDeploying)
            yield cleanup_deploying()


class UndeployAction(VComputeAction):
    context(IDeployed)

    action('undeploy')

    @defer.inlineCallbacks
    def _execute(self, cmd, args):
        name = yield db.get(self.context, '__name__')
        parent = yield db.get(self.context, '__parent__')

        submitter = IVirtualizationContainerSubmitter(parent)
        res = yield submitter.submit(IUndeployVM, name)
        cmd.write('%s\n' % (res,))

        @db.transact
        def finalize_vm():
            noLongerProvides(self.context, IDeployed)
            alsoProvides(self.context, IUndeployed)
            cmd.write("changed state from deployed to undeployed\n")

        yield finalize_vm()


class MigrateAction(VComputeAction):
    context(IVirtualCompute)

    action('migrate')

    def arguments(self):
        parser = VirtualConsoleArgumentParser()
        parser.add_argument('dest_path')
        parser.add_argument('-o', '--offline', action='store_true', default=False,
                            help="Force offline migration, shutdown VM before migrating")
        return parser

    @defer.inlineCallbacks
    def _check_vm(self, destination_vms):
        dest_submitter = IVirtualizationContainerSubmitter(destination_vms)
        vmlist = yield dest_submitter.submit(IListVMS)
        defer.returnValue((yield db.get(self.context, '__name__')) in map(lambda x: x['uuid'], vmlist))

    @defer.inlineCallbacks
    def _execute(self, cmd, args):

        @db.ro_transact
        def get_destination():
            return (args.__parent__ if IVirtualizationContainer.providedBy(args)
                    else cmd.traverse(args.dest_path))

        @db.ro_transact
        def get_hostname(target):
            return target.hostname

        def handle_error(msg):
            log.msg(msg, system='migrate')
            cmd.write(str(msg + '\n'))

        name = yield db.get(self.context, '__name__')
        source_vms = yield db.get(self.context, '__parent__')

        destination = yield get_destination()
        assert ICompute.providedBy(destination), 'Destination must be a Compute'
        assert not IVirtualCompute.providedBy(destination), 'Cannot migrate to a VM'
        destination_hostname = yield get_hostname(destination)
        destination_vms = follow_symlinks(destination['vms'])
        assert (yield db.get(destination_vms, 'backend')) == (yield db.get(source_vms, 'backend')),\
                'Destination backend is different from source'

        log.msg('Initiating migration for %s to %s' % (name, destination_hostname), system='migrate')

        if (yield self._check_vm(destination_vms)):
            handle_error('Failed migration of %s to %s: destination already contains this VM' % (
                name, destination_hostname))
            defer.returnValue(None)

        try:
            source_submitter = IVirtualizationContainerSubmitter(source_vms)
            yield source_submitter.submit(IMigrateVM, name, destination_hostname, (not args.offline), False)
        except OperationRemoteError as e:
            handle_error('Failed migration of %s to %s: remote error %s' % (
                name, destination_hostname, '\n%s' % e.remote_tb if e.remote_tb else ''))
            defer.returnValue(None)

        log.msg('Migration finished. Checking... %s' % destination_vms, system='migrate')

        if not (yield self._check_vm(destination_vms)):
            handle_error('Failed migration of %s to %s: VM not found in destination after migration '
                         'attempt' % (name, destination_hostname))
            defer.returnValue(None)
        else:
            log.msg('Migration finished successfully!', system='migrate')

            @db.transact
            def mv():
                machines = db.get_root()['oms_root']['machines']
                computes = db.get_root()['oms_root']['computes']
                try:
                    destination_compute = machines[destination.__name__]
                    vm_compute = follow_symlinks(computes[self.context.__name__])
                    dvms = follow_symlinks(destination_compute['vms'])
                    dvms.add(vm_compute)
                    log.msg('Model moved.', system='migrate')
                except IndexError:
                    log.msg('Model NOT moved: destination compute or vms do not exist', system='migrate')
                except KeyError:
                    log.msg('Model NOT moved: already moved by sync?', system='migrate')
            yield mv()


class InfoAction(VComputeAction):
    """This is a temporary command used to fetch realtime info"""
    context(IVirtualCompute)

    action('info')

    @db.ro_transact(proxy=False)
    def subject(self, *args, **kwargs):
        return tuple((self.context.__parent__,))

    @defer.inlineCallbacks
    def _execute(self, cmd, args):
        name = yield db.get(self.context, '__name__')
        parent = yield db.get(self.context, '__parent__')

        submitter = IVirtualizationContainerSubmitter(parent)
        try:
            # TODO: not efficient, improve
            for vm in (yield submitter.submit(IListVMS)):
                if vm['uuid'] == name:
                    max_key_len = max(len(key) for key in vm)
                    for key, value in vm.items():
                        cmd.write("%s %s\n" % ((key + ':').ljust(max_key_len), value))
        except Exception as e:
            cmd.write("%s\n" % format_error(e))


class StartComputeAction(VComputeAction):
    action('start')

    job = IStartVM


class ShutdownComputeAction(VComputeAction):
    action('shutdown')

    action_name = "shutting down"
    job = IShutdownVM


class DestroyComputeAction(VComputeAction):
    action('destroy')

    job = IDestroyVM


class SuspendComputeAction(VComputeAction):
    action('suspend')

    job = ISuspendVM


class ResumeAction(VComputeAction):
    action('resume')

    action_name = 'resuming'
    job = IResumeVM


class RebootAction(VComputeAction):
    action('reboot')

    job = IRebootVM


class SyncAction(ComputeAction):
    """Force compute sync"""
    action('sync')

    @db.ro_transact(proxy=False)
    def subject(self, *args, **kwargs):
        return tuple((self.context,))

    @defer.inlineCallbacks
    def _execute(self, cmd, args):
        default = yield self.default_console()

        yield self.sync_consoles()
        yield self.sync_hw()

        if any_stack_installed(self.context):
            yield self.ensure_vms()
            yield self.sync_templates()

        if IVirtualCompute.providedBy(self.context):
            yield self._sync_virtual()

        yield self._create_default_console(default)

        yield self.sync_vms()

    @db.ro_transact
    def default_console(self):
        return self._default_console()

    @db.assert_transact
    def _default_console(self):
        if self.context['consoles']:
            return None

        default = self.context.consoles['default']
        if default:
            return default.target.__name__

    @defer.inlineCallbacks
    def _create_default_console(self, default):
        @db.ro_transact
        def check():
            return not default or not self.context.consoles[default]

        @db.transact
        def create():
            self.create_default_console(default)

        if (yield check()):
            yield create()

    @db.assert_transact
    def create_default_console(self, default):
        if not default or not self.context.consoles[default]:
            if (IVirtualizationContainer.providedBy(self.context.__parent__)
                    and self.context.__parent__.backend == 'openvz'
                    and self.context.consoles['tty0']):
                default = 'tty0'
            else:
                default = 'ssh'

            self.context.consoles.add(Symlink('default', self.context.consoles[default]))

    @db.transact
    def sync_consoles(self):
        return self._sync_consoles()

    @db.assert_transact
    def _sync_consoles(self):
        if self.context['consoles'] and self.context.consoles['ssh']:
            return self.fixup_console_ip(self.context.consoles['ssh'])

        address = self.context.hostname
        try:
            if self.context.ipv4_address:
                address = self.context.ipv4_address.split('/')[0]
        except Exception:
            log.err(system='sync-consoles')
        ssh_console = SshConsole('ssh', 'root', address, 22)
        self.context.consoles.add(ssh_console)

    def fixup_console_ip(self, console):
        if self.context.ipv4_address:
            address = self.context.ipv4_address.split('/')[0]
            if console.hostname != address:
                console.hostname = address

    @defer.inlineCallbacks
    def _sync_virtual(self):
        parent = yield db.get(self.context, '__parent__')
        name = yield db.get(self.context, '__name__')
        submitter = IVirtualizationContainerSubmitter(parent)
        # TODO: sync VMs in parallel, eliminating most of the network roundtrip overhead
        vmlist = yield submitter.submit(IListVMS)
        for vm in vmlist:
            if vm['uuid'] == name:
                yield self._sync_vm(vm)

    @db.transact
    def _sync_vm(self, vm):
        return self.sync_vm(vm)

    @db.assert_transact
    def sync_vm(self, vm):
        compute = TmpObj(self.context)

        compute.state = unicode(vm['state'])
        compute.effective_state = compute.state

        # Ensure IDeployed marker is set, unless not in another state
        if not IDeployed.providedBy(compute):
            noLongerProvides(self.context, IUndeployed)
            noLongerProvides(self.context, IDeploying)
            alsoProvides(self.context, IDeployed)

        for idx, console in enumerate(vm['consoles']):
            if console['type'] == 'pty' and not self.context.consoles['tty%s' % idx]:
                self.context.consoles.add(TtyConsole('tty%s' % idx, console['pty']))
            if console['type'] == 'openvz' and not self.context.consoles['tty%s' % idx]:
                self.context.consoles.add(OpenVzConsole('tty%s' % idx, console['cid']))
            if console['type'] == 'vnc' and not self.context.consoles['vnc']:
                self.context.consoles.add(VncConsole(
                    self.context.__parent__.__parent__.hostname, int(console['port'])))

        # XXX TODO: handle removal of consoles when they are no longer reported from upstream
        # networks
        for interface in vm['interfaces']:
            if not self.context.interfaces[interface['name']]:
                iface = NetworkInterface(interface['name'], None, interface['mac'], 'active')
                if 'ipv4_address' in interface:
                    iface.ipv4_address = interface['ipv4_address']
                self.context.interfaces.add(iface)

        # XXX TODO: handle removal of interfaces when they are no longer reported from upstream
        # XXX hack, openvz specific
        compute.cpu_info = self.context.__parent__.__parent__.cpu_info
        compute.memory = vm['memory']

        diskspace = dict((unicode(k), v) for k, v in vm['diskspace'].items())
        diskspace[u'total'] = sum([0.0] + vm['diskspace'].values())
        # round diskspace values
        for i in diskspace:
            diskspace[i] = round(diskspace[i], 2)

        compute.diskspace = diskspace

        if compute.effective_state != 'active':
            compute.uptime = None
        else:
            compute.uptime = get_f(vm, 'uptime')

        compute.apply()

    @defer.inlineCallbacks
    def sync_hw(self):
        if not any_stack_installed(self.context):
            return

        try:
            info = yield IGetComputeInfo(self.context).run()
            uptime = yield IGetHWUptime(self.context).run()
            disk_usage = yield IGetDiskUsage(self.context).run()
        except OperationRemoteError as e:
            log.msg(e.message, system='sync-hw')
            if e.remote_tb:
                log.msg(e.remote_tb, system='sync-hw')
            return

        # TODO: Improve error handling
        def disk_info(aspect):
            res = dict((unicode(k), round(float(v[aspect]) / 1024, 2))
                       for k, v in disk_usage.items()
                       if v['device'].startswith('/dev/'))
            res[u'total'] = sum([0.0] + res.values())
            return res

        routes = yield IGetRoutes(self.context).run()

        yield self._sync_hw(info, disk_info('total'), disk_info('used'), routes, uptime)

    @db.transact
    def _sync_hw(self, info, disk_space, disk_usage, routes, uptime):
        if any((not info, 'cpuModel' not in info, 'kernelVersion' not in info)):
            log.msg('Nothing to update: info does not include required data', system='sync-hw')
            return

        if IVirtualCompute.providedBy(self.context):
            self.context.cpu_info = self.context.__parent__.__parent__.cpu_info
        else:
            self.context.cpu_info = unicode(info['cpuModel'])

        self.context.architecture = (unicode(info['platform']), u'linux', self.distro(info))
        self.context.kernel = unicode(info['kernelVersion'])
        self.context.memory = info['systemMemory']
        self.context.num_cores = info['numCpus']
        self.context.os_release = unicode(info['os'])
        self.context.swap_size = info['systemSwap']
        self.context.diskspace = disk_space
        self.context.diskspace_usage = disk_usage
        self.context.template = u'Hardware node'
        self.context.uptime = uptime

        # XXX TODO: handle removal of routes
        for i in routes:
            destination = netaddr.IPNetwork('%s/%s' % (i['destination'], i['netmask']))
            route_name = str(destination.cidr).replace('/', '_')

            if self.context.routes[route_name]:
                continue

            gateway = netaddr.IPAddress(i['router'])

            route = NetworkRoute()
            route.destination = str(destination.cidr)
            route.gateway = str(gateway)
            route.flags = i['flags']
            route.metrics = int(i['metrics'])
            route.__name__ = route_name

            interface = self.context.interfaces[i['interface']]
            if interface:
                route.add(Symlink('interface', interface))

            self.context.routes.add(route)

    def distro(self, info):
        if 'os' in info:
            return unicode(info['os'].split()[0])
        else:
            return 'Unknown'

    @defer.inlineCallbacks
    def ensure_vms(self):
        if not follow_symlinks(self.context['vms']) and any_stack_installed(self.context):
            vms_types = yield IGetVirtualizationContainers(self.context).run()
            if vms_types:
                url_to_backend_type = dict((v, k) for k, v in backends.items())
                backend_type = url_to_backend_type[vms_types[0]]

                @db.transact
                def add_container(backend_type):
                    vms = VirtualizationContainer(unicode(backend_type))
                    self.context.add(vms)
                    if not self.context['vms']:
                        self.context.add(Symlink('vms', self.context[vms.__name__]))

                yield add_container(backend_type)

    def sync_vms(self):
        vms = follow_symlinks(self.context['vms'])
        if vms:
            return SyncVmsAction(vms).execute(DetachedProtocol(), object())

    @defer.inlineCallbacks
    def sync_templates(self):
        if not follow_symlinks(self.context['vms']):
            return

        submitter = IVirtualizationContainerSubmitter(follow_symlinks(self.context['vms']))
        templates = yield submitter.submit(IGetLocalTemplates)

        if not templates:
            return

        @db.transact
        def update_templates():
            template_container = self.context.templates
            for i in templates:
                name = i['template_name']
                if not template_container['by-name'][name]:
                    template_container.add(Template(unicode(name), get_u(i, 'domain_type')))

                template = template_container['by-name'][name].target
                template.cores = (get_i(i, 'vcpu_min'),
                                  get_i(i, 'vcpu'),
                                  max(-1, get_i(i, 'vcpu_max')))
                template.memory = (get_f(i, 'memory_min'),
                                   get_f(i, 'memory'),
                                   max(-1.0, get_f(i, 'memory_max')))
                template.swap = (get_f(i, 'swap_min'),
                                 get_f(i, 'swap'),
                                 max(-1.0, get_f(i, 'swap_max')))
                template.disk = (get_f(i, 'disk_min'),
                                 get_f(i, 'disk'),
                                 max(-1.0, get_f(i, 'disk_max')))
                template.nameserver = get_u(i, 'nameserver')
                template.password = get_u(i, 'passwd')
                template.cpu_limit = (get_i(i, 'vcpulimit_min'),
                                      get_i(i, 'vcpulimit'))
                template.ip = get_u(i, 'ip_address')

            # delete templates no more offered upstream
            template_names = template_container['by-name'].listnames()
            for i in set(template_names).difference(i['template_name'] for i in templates):
                template_container.remove(follow_symlinks(template_container['by-name'][i]))

        yield update_templates()


@subscribe(ICompute, IModelModifiedEvent)
@defer.inlineCallbacks
def handle_compute_state_change_request(compute, event):

    if not event.modified.get('state', None):
        defer.returnValue(None)

    def get_action(original, modified):
        action_mapping = {'inactive': {'active': IStartVM},
                          'suspended': {'active': IResumeVM},
                          'active': {'inactive': IShutdownVM,
                                     'suspended': ISuspendVM}}

        action = action_mapping.get(original, {}).get(modified, None)
        return action

    original = event.original['state']
    modified = event.modified['state']
    action = get_action(original, modified)

    if not action:
        defer.returnValue(None)

    submitter = IVirtualizationContainerSubmitter(compute.__parent__)
    try:
        yield submitter.submit(action, compute.__name__)
    except Exception:
        compute.effective_state = event.original['state']
        raise
    else:
        compute.effective_state = event.modified['state']

    handle(compute, ModelModifiedEvent({'effective_state': event.original['state']},
                                       {'effective_state': compute.effective_state}))


@subscribe(IVirtualCompute, IModelDeletedEvent)
def delete_virtual_compute(model, event):
    if not ICompute.providedBy(model.__parent__.__parent__):
        return

    if IDeployed.providedBy(model):
        log.msg('deleting compute %s which is in IDeployed state, shutting down and '
                'undeploying first' % model.hostname, system='compute_backend')
        blocking_yield(DestroyComputeAction(model).execute(DetachedProtocol(), object()), timeout=20000)
        blocking_yield(UndeployAction(model).execute(DetachedProtocol(), object()), timeout=20000)
    else:
        log.msg('deleting compute %s which is already in IUndeployed state' %
                model.hostname, system='compute_backend')


@subscribe(IVirtualCompute, IModelCreatedEvent)
def create_virtual_compute(model, event):
    # TODO: maybe raise an exception here instead?
    if not IVirtualizationContainer.providedBy(model.__parent__):
        return

    if not ICompute.providedBy(model.__parent__.__parent__):
        return

    if IDeployed.providedBy(model):
        return

    log.msg('Deploying VM "%s"' % model, system='deploy')
    exception_logger(DeployAction(model).execute)(DetachedProtocol(), object())


@subscribe(IVirtualCompute, IModelModifiedEvent)
@defer.inlineCallbacks
def handle_virtual_compute_config_change_request(compute, event):
    update_param_whitelist = ['cpu_limit',
                              'memory',
                              'num_cores',
                              'swap_size']

    params_to_update = filter(lambda (k, v): k in update_param_whitelist, event.modified.iteritems())

    if len(params_to_update) == 0:
        return

    update_values = [v for k, v in sorted(params_to_update, key=lambda (k, v): k)]

    submitter = IVirtualizationContainerSubmitter((yield db.get(compute, '__parent__')))
    try:
        yield submitter.submit(IUpdateVM, (yield db.get(compute, '__name__')), *update_values)
    except Exception:
        for mk, mv in event.modified.iteritems():
            setattr(compute, mk, event.original[mk])
        raise
