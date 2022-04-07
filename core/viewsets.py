import djstripe.settings
import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from rest_framework import viewsets, status
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response

from core.permissions import ReadOnlyAnonymous
from .tasks import provision_service, calculate_inventory, start_vm, stop_vm, reboot_vm, \
    reset_vm, shutdown_vm, provision_billing, get_vm_status, get_cluster_resources, get_vm_ips, get_vm_tasks

if settings.STRIPE_LIVE_SECRET_KEY or settings.STRIPE_TEST_SECRET_KEY:
    from djstripe.models import Session, Customer, Product, Price
from rest_framework.decorators import action
from .serializers import \
    ServiceSerializer, \
    IPPoolSerializer, \
    NewServiceSerializer, \
    ClusterSerializer, \
    InventorySerializer, \
    CustomerServiceSerializer, \
    CustomerServiceListSerializer, \
    DashboardSummarySerializer, \
    GenericActionSerializer

from .models import \
    IPPool, \
    IP, \
    Plan, \
    Service, \
    Template, \
    Cluster, \
    Node, \
    Inventory, \
    DashboardSummary

import random
from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException
import string

UserModel = get_user_model()


class DynamicPageModelViewSet(viewsets.ModelViewSet):
    def paginate_queryset(self, queryset):
        if 'no_page' in self.request.query_params:
            return None

        return super().paginate_queryset(queryset)


class MultiSerializerViewSetMixin(object):
    def get_serializer_class(self):
        try:
            user = self.request.user
        except AttributeError:
            return self.default_serializer_class
        if user.is_staff:
            action_classes = self.admin_serializer_action_classes
        else:
            action_classes = self.serializer_action_classes
        try:
            return action_classes[self.action]
        except (KeyError, AttributeError):
            return action_classes['default']


class ClusterViewSet(MultiSerializerViewSetMixin, DynamicPageModelViewSet):
    permission_classes = [IsAdminUser]
    queryset = Cluster.objects.order_by('pk')
    default_serializer_class = ClusterSerializer
    admin_serializer_action_classes = {
        'list': ClusterSerializer,
        'retrieve': ClusterSerializer,
        'update': ClusterSerializer,
        'create': ClusterSerializer,
        'default': ClusterSerializer
    }
    serializer_action_classes = {}

    @action(methods=['get'], detail=True)
    def nodes(self, request, pk=None):
        stats = get_cluster_resources(pk=pk, query_type="node")
        return Response(stats, status=202)

    @action(methods=['get'], detail=True)
    def vms(self, request, pk=None):
        stats = get_cluster_resources(pk=pk, query_type="vm")
        return Response(stats, status=202)

    @action(methods=['get'], detail=True)
    def disks(self, request, pk=None):
        stats = get_cluster_resources(pk=pk, query_type="storage")
        return Response(stats, status=202)


class IPPoolViewSet(DynamicPageModelViewSet):
    permission_classes = [IsAdminUser]
    queryset = IPPool.objects.order_by('pk')
    serializer_class = IPPoolSerializer

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance:
            used_ips = IP.objects.filter(pool=instance, owner__isnull=False).count()
            if used_ips > 0:
                return Response(data={'message': "IPs in pool are currently in use"},
                                status=status.HTTP_400_BAD_REQUEST)
        return super(IPPoolViewSet, self).destroy(request, *args, **kwargs)


class InventoryViewSet(DynamicPageModelViewSet):
    permission_classes = [IsAdminUser | ReadOnlyAnonymous]
    queryset = Inventory.objects.order_by('pk')
    serializer_class = InventorySerializer

    @action(methods=['post'], detail=False)
    def calculate(self, request):
        task = calculate_inventory.delay()
        return Response({"task_id": task.id}, status=202)


class ServiceViewSet(MultiSerializerViewSetMixin, DynamicPageModelViewSet):
    permission_classes = [IsAdminUser | IsAuthenticated]

    default_serializer_class = ServiceSerializer
    admin_serializer_action_classes = {
        'list': ServiceSerializer,
        'retrieve': ServiceSerializer,
        'update': ServiceSerializer,
        'create': NewServiceSerializer,
        'default': GenericActionSerializer,
        'metadata': ServiceSerializer,
    }
    serializer_action_classes = {
        'list': CustomerServiceListSerializer,
        'retrieve': CustomerServiceSerializer,
        'update': CustomerServiceSerializer,
        'create': CustomerServiceSerializer,
        'default': GenericActionSerializer,
        'metadata': CustomerServiceSerializer
    }

    @action(methods=['post'], detail=True)
    def start(self, request, pk=None):
        task = start_vm.delay(pk)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['post'], detail=True)
    def shutdown(self, request, pk=None):
        task = shutdown_vm.delay(pk)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['post'], detail=True)
    def reset(self, request, pk=None):
        task = reset_vm.delay(pk)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['post'], detail=True)
    def stop(self, request, pk=None):
        task = stop_vm.delay(pk)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['post'], detail=True)
    def reboot(self, request, pk=None):
        task = reboot_vm.delay(pk)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['post'], detail=True)
    def status(self, request, pk=None):
        stats = get_vm_status(pk)
        return Response(stats, status=202)

    @action(methods=['post'], detail=True)
    def provision(self, request, pk=None):
        task = provision_service.delay(service_id=pk, password=None)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['post'], detail=True)
    def provision_billing(self, request, pk=None):
        task = provision_billing.delay(pk)
        return Response({"task_id": task.id}, status=202)

    @action(methods=['get'], detail=True)
    def ips(self, request, pk=None):
        ips = get_vm_ips(pk)
        return Response(ips, status=202)

    @action(methods=['get'], detail=True)
    def tasks(self, request, pk=None):
        tasks = get_vm_tasks(pk)
        return Response(tasks, status=202)

    @action(detail=True)
    def billing(self, request, pk=None):
        try:
            service_id = pk
        except KeyError:
            raise
        service = Service.objects.get(id=service_id)
        customer = Customer.objects.get(subscriber_id=service.owner.id)
        try:
            session = Session.objects.get(customer=customer, client_reference_id=service_id)
        except Session.DoesNotExist:
            product = Product.objects.get(livemode=djstripe.settings.STRIPE_LIVE_MODE,
                                          name=service.plan.name,
                                          active=True)
            price = Price.objects.get(livemode=djstripe.settings.STRIPE_LIVE_MODE,
                                      active=True,
                                      product=product,
                                      unit_amount=int(service.plan.price * 100),
                                      recurring__interval=service.plan.period,
                                      recurring__interval_count=service.plan.term
                                      )
            site = Site.objects.get(pk=settings.SITE_ID)
            data = {
                'payment_method_types': ['card'],
                'client_reference_id': service_id,
                'customer': customer.id,
                'line_items': [{
                    'price': price.id,
                    'quantity': 1,
                }],
                'mode': 'subscription',
                'success_url': f'https://{site.domain}/services/{service_id}/',
                'cancel_url': f'https://{site.domain}/services/{service_id}/',
                'metadata': {
                    'inveterate_id': service_id
                },
                "subscription_data": {
                    'metadata': {
                        'inveterate_id': service_id
                    }
                }
            }
            stripe.api_key = djstripe.settings.STRIPE_SECRET_KEY
            session = stripe.checkout.Session.create(**data)
            Session._create_from_stripe_object(data=session)
        return Response(
            {"type": "stripe",
             "key": djstripe.settings.STRIPE_PUBLIC_KEY,
             "service_id": service_id,
             "sessionid": session.id})

    @action(methods=['post'], detail=True)
    def console_login(self, request, pk=None):
        try:
            service_id = pk
        except KeyError:
            raise
        else:
            service = Service.objects.get(id=pk)
            proxmox_user = f'inveterate{service.owner_id}'
            password = ''.join(
                random.SystemRandom().choice(string.ascii_letters + string.digits + string.punctuation) for _ in
                range(10))
            proxmox = ProxmoxAPI(service.node.cluster.host, user=service.node.cluster.user, token_name='inveterate',
                                 token_value=service.node.cluster.key,
                                 verify_ssl=False, port=8006)

            try:
                proxmox.access.users.post(userid=f"{proxmox_user}@pve", password=password)
            except ResourceException as e:
                if "already exists" in str(e):
                    proxmox.access.users(f"{proxmox_user}@pve").delete()
                    proxmox.access.users.post(userid=f"{proxmox_user}@pve", password=password)
            proxmox.access.acl.put(path=f"/vms/{service.machine_id}", roles=["PVEVMConsole"],
                                   users=[f"{proxmox_user}@pve"])
            type = "lxc" if service.service_plan.type == "lxc" else "qemu"
            response = Response(
                {"username": f"{proxmox_user}@pve",
                 "password": password,
                 "node": service.node.name,
                 "machine": service.machine_id,
                 "type": type}
            )
        return response

    def get_queryset(self):
        if self.request.user.is_staff:
            return Service.objects.all().exclude(status='destroyed').order_by('pk')
        return Service.objects.filter(owner=self.request.user).exclude(status='destroyed').order_by('pk')


class DashboardViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAdminUser]
    queryset = DashboardSummary.objects.order_by('pk')
    serializer_class = DashboardSummarySerializer

    @action(methods=['get'], detail=False)
    def summary(self, request):
        user_count = UserModel.objects.count()
        plan_count = Plan.objects.count()
        ip_count = IP.objects.count()
        template_count = Template.objects.count()
        service_count = Service.objects.count()
        node_count = Node.objects.count()
        data = {
            'users': user_count,
            'plans': plan_count,
            'ips': ip_count,
            'templates': template_count,
            'services': service_count,
            'nodes': node_count
        }
        return Response(data, status=202)
