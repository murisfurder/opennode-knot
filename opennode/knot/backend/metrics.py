
from grokcore.component import Adapter, context
import time
from twisted.internet import defer
from zope.component import provideSubscriptionAdapter, queryAdapter
from zope.interface import implements, Interface

from opennode.knot.backend.v12ncontainer import IVirtualizationContainerSubmitter
from opennode.knot.backend.operation import IStackInstalled, IGetGuestMetrics
from opennode.oms.config import get_config
from opennode.oms.model.model.proc import IProcess, Proc, DaemonProcess
from opennode.oms.util import subscription_factory, async_sleep
from opennode.oms.zodb import db
from opennode.oms.model.model.symlink import follow_symlinks


class IMetricsGatherer(Interface):
    def gather():
        """Gathers metrics for some object"""


class MetricsDaemonProcess(DaemonProcess):
    implements(IProcess)

    __name__ = "metrics"

    def __init__(self):
        super(MetricsDaemonProcess, self).__init__()

        config = get_config()
        self.interval = config.getint('metrics', 'interval')

    @defer.inlineCallbacks
    def run(self):
        while True:
            yield async_sleep(self.interval)
            try:
                # Currently we have special codes for gathering info about machines
                # hostinginv VM, in future here we'll traverse the whole zodb and search for gatherers
                # and maintain the gatherers via add/remove events.
                if not self.paused:
                    yield self.gather_machines()
            except Exception:
                import traceback
                traceback.print_exc()
                pass

    def log(self, msg):
        print "[metrics] %s" % (msg, )

    @defer.inlineCallbacks
    def gather_machines(self):
        @db.ro_transact
        def get_gatherers():
            res = []

            oms_root = db.get_root()['oms_root']
            for i in [follow_symlinks(i) for i in oms_root['computes'].listcontent()]:
                adapter = queryAdapter(i, IMetricsGatherer)
                if adapter:
                    res.append(adapter)

            return res

        for i in (yield get_gatherers()):
            try:
                yield i.gather()
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.log("Got exception when gathering metrics compute '%s': %s" % (i.context, e))


provideSubscriptionAdapter(subscription_factory(MetricsDaemonProcess), adapts=(Proc,))


class VirtualComputeMetricGatherer(Adapter):
    """Gathers VM metrics through functionality exposed by the host compute via func."""

    implements(IMetricsGatherer)
    context(IStackInstalled)

    @defer.inlineCallbacks
    def gather(self):
        yield self.gather_vms()
        yield self.gather_phy()

    @defer.inlineCallbacks
    def gather_vms(self):

        @db.ro_transact
        def get_vms():
            return self.context['vms']
        vms = yield get_vms()

        # get the metrics for all running VMS
        if not vms or self.context.state != u'active':
            return

        metrics = yield IVirtualizationContainerSubmitter(vms).submit(IGetGuestMetrics)

        timestamp = int(time.time() * 1000)

        # db transact is needed only to traverse the zodb.
        @db.ro_transact
        def get_streams():
            streams = []
            for uuid, data in metrics.items():
                vm = vms[uuid]
                if vm:
                    vm_metrics = vm['metrics']
                    if vm_metrics:
                        for k in data:
                            if vm_metrics[k]:
                                streams.append((IStream(vm_metrics[k]), (timestamp, data[k])))
            return streams

        # streams could defer the data appending but we don't care
        for stream, data_point in (yield get_streams()):
            stream.add(data_point)

    @defer.inlineCallbacks
    def gather_phy(self):
        try:
            data = yield IGetHostMetrics(self.context).run()

            timestamp = int(time.time() * 1000)

            # db transact is needed only to traverse the zodb.
            @db.ro_transact
            def get_streams():
                streams = []
                host_metrics = self.context['metrics']
                if host_metrics:
                    for k in data:
                        if host_metrics[k]:
                            streams.append((IStream(host_metrics[k]), (timestamp, data[k])))

                return streams

            for stream, data_point in (yield get_streams()):
                stream.add(data_point)

        except Exception as e:
            if get_config().getboolean('debug', 'print_exceptions'):
                print "[metrics] cannot gather phy metrics", e
