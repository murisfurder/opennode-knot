from __future__ import absolute_import

import traceback

from twisted.internet import defer

from grokcore.component import subscribe
from grokcore.component import context, subscribe, baseclass, Adapter

from zope.interface import implements
from zope.component import provideSubscriptionAdapter

from opennode.oms.model.model.proc import IProcess, Proc, DaemonProcess
from opennode.oms.model.model.actions import Action, action
from opennode.oms.model.model.symlink import follow_symlinks
from opennode.oms.model.form import IModelCreatedEvent, IModelDeletedEvent
from opennode.oms.util import subscription_factory, async_sleep, blocking_yield
from opennode.oms.endpoint.ssh.detached import DetachedProtocol
from opennode.oms.zodb import db
from opennode.oms.config import get_config

from opennode.knot.model.zabbix import IZabbixServer
from opennode.knot.model.compute import ICompute
from opennode.knot.model.hangar import IHangar
from opennode.knot.backend.zabbix_api import ZabbixAPI


class ZabbixSyncAction(Action):
    """Synchronize zabbix server model with the targeted server."""
    context(IZabbixServer)

    action('sync')

    @db.transact
    def execute(self, cmd, args):
        blocking_yield(self._execute(cmd, args))

    @defer.inlineCallbacks
    def _execute(self, cmd, args):
        try:
            cmd.write('Executed a call to zabbix server')
            zapi = ZabbixAPI(server=self.context.url, path="", log_level=6)
            yield zapi.login(self.context.username, self.context.password)
            existing_groups = yield zapi.hostgroup.get({"output": 'extend'})
            if len(existing_groups) > 0:
                self.context.hostgroups = {}
            for group in existing_groups:
                self.context.hostgroups[int(group['groupid'])] = group['name']
        except Exception as e:
            cmd.write("%s\n" % (": ".join(msg for msg in e.args if isinstance(msg, str) and not msg.startswith('  File "/'))))


class ZabbixSyncDaemonProcess(DaemonProcess):
    implements(IProcess)

    __name__ = "zabbix-sync"

    def __init__(self):
        super(ZabbixSyncDaemonProcess, self).__init__()

        self.config = get_config()
        self.interval = self.config.getint('zabbix', 'sync-interval')

    @defer.inlineCallbacks
    def run(self):
        while True:
            try:
                if not self.paused:
                    self.log("yielding zabbix sync")
                    yield self.sync()
                    self.log("zabbix sync yielded")
            except Exception:
                if get_config().getboolean('debug', 'print_exceptions'):
                    traceback.print_exc()

            yield async_sleep(self.interval)

    def log(self, msg):
        import threading
        print "[zabbix-sync] (%s) %s" % (threading.current_thread(), msg)

    @defer.inlineCallbacks
    def sync(self):
        self.log("syncing zabbix")

        @db.ro_transact
        def get_zabbix_server():
            # refresh server from 
            get_config
            res = []
            oms_root = db.get_root()['oms_root']
            for i in [follow_symlinks(i) for i in oms_root['zabbix'].listcontent()]:
                if IZabbixServer.providedBy(i):
                    res.append((i, i.url))
            return res

        sync_actions = []

        for i, hostname in (yield get_zabbix_server()):
            action = ZabbixSyncAction(i)
            sync_actions.append((hostname, action.execute(DetachedProtocol(), object())))

        self.log("waiting for background zabbix sync tasks")
        # wait for all async synchronization tasks to finish
        for c, deferred in sync_actions:
            try:
                yield deferred
            except Exception as e:
                self.log("Got exception when syncing zabbix server '%s': %s" % (c, e))
                if get_config().getboolean('debug', 'print_exceptions'):
                    traceback.print_exc()
            else:
                self.log("Syncing was ok for Zabbix server: '%s'" % c)

        self.log("synced")


provideSubscriptionAdapter(subscription_factory(ZabbixSyncDaemonProcess), adapts=(Proc,))


@subscribe(ICompute, IModelCreatedEvent)
def add_compute_to_zabbix(model, event):
    if IHangar.providedBy(model.__parent__):
        return
    # FILL IT
    print "TODO: add %s to zabbix" % (model,)


@subscribe(ICompute, IModelDeletedEvent)
def remove_compute_from_zabbix(model, event):
    if IHangar.providedBy(model.__parent__):
        return
    # FILL IT
    # exception thrown will prevent deletion
    print "TODO: remove %s from zabbix" % (model,)
