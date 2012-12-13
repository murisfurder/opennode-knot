import traceback

from datetime import datetime
from grokcore.component.directive import context
from twisted.internet import defer
from zope.component import provideSubscriptionAdapter
from zope.interface import implements

from opennode.knot.backend.compute import SyncAction
from opennode.knot.backend import func as func_backend
from opennode.knot.backend import salt as salt_backend
from opennode.knot.model.compute import ICompute
from opennode.knot.model.compute import IManageable
from opennode.knot.utils.icmp import ping
from opennode.knot.utils.logging import log
from opennode.oms.config import get_config
from opennode.oms.endpoint.ssh.detached import DetachedProtocol
from opennode.oms.model.model.actions import Action, action
from opennode.oms.model.model.proc import IProcess, Proc, DaemonProcess
from opennode.oms.model.model.symlink import follow_symlinks
from opennode.oms.util import subscription_factory, async_sleep
from opennode.oms.zodb import db


class PingCheckAction(Action):
    """Check if a Compute responds to ICMP request from OMS."""
    context(ICompute)

    action('ping-check')

    def __init__(self, *args, **kwargs):
        super(PingCheckAction, self).__init__(*args, **kwargs)
        config = get_config()
        self.mem_limit = config.getint('pingcheck', 'mem_limit')

    @defer.inlineCallbacks
    def execute(self, cmd, args):
        yield self._execute(cmd, args)

    @db.transact
    def _execute(self, cmd, args):
        address = self.context.hostname.encode('utf-8')
        res = ping(address)
        self.context.last_ping = (res == 1)
        self.context.pingcheck.append({'timestamp': datetime.utcnow(),
                                       'result': res})
        history_len = len(self.context.pingcheck)
        if history_len > self.mem_limit:
            del self.context.pingcheck[:-self.mem_limit]

        ping_results = map(lambda i: i['result'] == 1, self.context.pingcheck[:3])

        self.context.suspicious = not all(ping_results)
        self.context.failure = not any(ping_results)


class PingCheckDaemonProcess(DaemonProcess):
    implements(IProcess)

    __name__ = "ping-check"

    def __init__(self):
        super(PingCheckDaemonProcess, self).__init__()

        config = get_config()
        self.interval = config.getint('pingcheck', 'interval')

    @defer.inlineCallbacks
    def run(self):
        while True:
            try:
                if not self.paused:
                    yield self.ping_check()
            except Exception:
                if get_config().getboolean('debug', 'print_exceptions'):
                    traceback.print_exc()

            yield async_sleep(self.interval)

    @defer.inlineCallbacks
    def ping_check(self):

        @db.ro_transact
        def get_computes():
            oms_root = db.get_root()['oms_root']
            res = [(i, i.hostname)
                   for i in map(follow_symlinks,
                                oms_root['computes'].listcontent())
                   if ICompute.providedBy(i)]

            return res

        ping_actions = []
        for i, hostname in (yield get_computes()):
            action = PingCheckAction(i)
            ping_actions.append((hostname,
                                 action.execute(DetachedProtocol(), object())))

        # wait for all async synchronization tasks to finish
        for c, deferred in ping_actions:
            try:
                yield deferred
            except Exception as e:
                log("Got exception when pinging compute '%s': %s" % (c, e), 'ping-check')
                if get_config().getboolean('debug', 'print_exceptions'):
                    traceback.print_exc()


provideSubscriptionAdapter(subscription_factory(PingCheckDaemonProcess), adapts=(Proc,))

@db.ro_transact
def get_machines():
    oms_root = db.get_root()['oms_root']
    machines = (follow_symlinks(j) for j in oms_root['machines'].listcontent())
    res = [(i, i.hostname) for i in machines
           if ICompute.providedBy(i) and IManageable.providedBy(i)]
    return res


@db.ro_transact
def delete_machines(delete_list):
    oms_machines = db.get_root()['oms_root']['machines']

    for host, hostname in delete_list:
        del oms_machines[host.__name__]


class SyncDaemonProcess(DaemonProcess):
    implements(IProcess)

    __name__ = "sync"

    def __init__(self):
        super(SyncDaemonProcess, self).__init__()

        config = get_config()
        self.interval = config.getint('sync', 'interval')

    @defer.inlineCallbacks
    def run(self):
        while True:
            try:
                if not self.paused:
                    log("yielding sync", 'sync')
                    yield self.sync()
                    log("sync yielded", 'sync')
            except Exception:
                if get_config().getboolean('debug', 'print_exceptions'):
                    traceback.print_exc()

            yield async_sleep(self.interval)

    @defer.inlineCallbacks
    def sync(self):
        log("syncing", 'sync')

        # This will help resolve issues like ON-751
        log('Machines before cleanup: %s' % (yield get_machines()), 'sync')
        accepted_salt = salt_backend.machines.get_accepted_machines()
        accepted_func = func_backend.machines.get_accepted_machines()

        accepted = accepted_salt.union(set(accepted_func))
        hosts_to_delete = [(host, hostname) for host, hostname in (yield get_machines())
                           if hostname not in accepted]
        log('Hosts to delete: %s' % (hosts_to_delete), 'sync')
        # cleanup machines not known from func or salt
        yield delete_machines(hosts_to_delete)
        yield salt_backend.machines.import_machines(accepted_salt)
        yield func_backend.machines.import_machines(accepted_func)

        sync_actions = (yield self._getSyncActions())

        log("waiting for background sync tasks", 'sync')

        # wait for all async synchronization tasks to finish
        for c, deferred in sync_actions:
            try:
                yield deferred
            except Exception as e:
                log("Got exception when syncing compute '%s': %s" % (c, e), 'sync')
                if get_config().getboolean('debug', 'print_exceptions'):
                    traceback.print_exc()
            else:
                log("Syncing was ok for compute: '%s'" % c, 'sync')

        log("synced", 'sync')

    @defer.inlineCallbacks
    def _getSyncActions(self):
        sync_actions = []
        for i, hostname in (yield get_machines()):
            action = SyncAction(i)
            sync_actions.append((hostname, action.execute(DetachedProtocol(), object())))

        defer.returnValue(sync_actions)

provideSubscriptionAdapter(subscription_factory(SyncDaemonProcess), adapts=(Proc,))
