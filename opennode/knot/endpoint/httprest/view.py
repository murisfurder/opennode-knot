import json

from grokcore.component import context

from opennode.knot.model import Machines, Compute
from opennode.knot.model.actions import ActionsContainer
from opennode.knot.model.hangar import Hangar
from opennode.knot.model.virtualizationcontainer import VirtualizationContainer
from opennode.oms.model.form import ApplyRawData
from opennode.oms.endpoint.httprest.view import ContainerView
from opennode.oms.endpoint.httprest.base import IHttpRestView
from opennode.oms.endpoint.httprest.root import BadRequest


class MachinesView(ContainerView):
    context(Machines)

    def blacklisted(self, item):
        return super(MachinesView, self).blacklisted(item) or isinstance(item, Hangar)


class VirtualizationContainerView(ContainerView):
    context(VirtualizationContainer)

    def blacklisted(self, item):
        return (super(VirtualizationContainerView, self).blacklisted(item)
                or isinstance(item, ActionsContainer))

    def render_POST(self, request):
        try:
            data = json.load(request.content)
        except ValueError:
            raise BadRequest("Input data could not be parsed")

        if not isinstance(data, dict):
            raise BadRequest("Input data must be a dictionary")

        # cleanup hacks
        data['state'] = 'active' if data['start_on_boot'] else 'inactive'
        if data.get('diskspace'):
            data['diskspace'] = {'root': data['diskspace']}

        # XXX: ONC should send us a 'nameserver' list instead of this hackish dns1,dns2
        nameservers = []
        for k in ['dns1', 'dns2']:
            if data.get(k, None):
                nameservers.append(data[k])
        data['nameservers'] = nameservers

        # XXX: ONC should send 'autostart'
        # XXX: since it's a IVirtualCompute specific field it cannot be entered during object creation
        #      because `form` doesn't support yet optionl interfaces.
        # data['autostart'] = data['start_on_boot']
        autostart = data['start_on_boot']

        for k in ['dns1', 'dns2', 'root_password', 'root_password_repeat', 'network-type', 'start_on_boot']:
             if data.has_key(k):
                 del data[k]

        form = ApplyRawData(data, model=Compute)
        if form.errors or not data.get('template'):
            template_error = [dict(id='template', msg="missing value")] if not data.get('template') else []
            return {
                'success': False,
                'errors': [dict(id=k, msg=v) for k, v in form.error_dict().items()] + template_error
                }

        compute = form.create()

        compute.autostart = autostart

        self.context.add(compute)

        data['id'] = compute.__name__

        return {
            'success': True,
            'result': IHttpRestView(compute).render_GET(request)
        }


class ComputeView(ContainerView):
    context(Compute)

    def render_recursive(self, request, *args, **kwargs):
        ret = super(ComputeView, self).render_recursive(request, *args, **kwargs)

        ret.update({
            'features': [i.__name__ for i in self.context.implemented_interfaces()],
            'startup_timestamp': self.context.startup_timestamp,
        })
        return ret
