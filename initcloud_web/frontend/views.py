#-*- coding=utf-8 -*-

import logging

from django.conf import settings
from django.views.generic import View
from django.shortcuts import render, redirect
from django.http import (HttpResponseRedirect,
                         JsonResponse,
                         HttpResponseForbidden)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.models import User
from django.contrib import messages
from django.utils.translation import ugettext_lazy as _
from django.utils.translation import ugettext
from django.utils import timezone
from django.core.urlresolvers import reverse
from django.template.loader import get_template
from django.template import Context

from cloud.tasks import link_user_to_dc_task, send_mail
from cloud.api import keystone
from biz.idc.models import UserDataCenter, DataCenter
from cloud.cloud_utils import create_rc_by_dc
from biz.account.models import Notification, ActivateUrl, UserProxy, Operation
from biz.idc.models import DataCenter, UserDataCenter as UDC
from biz.common.decorators import superuser_required
from frontend.forms import CloudUserCreateForm

LOG = logging.getLogger(__name__)


@login_required
def index(request):
    if request.user.is_superuser:
        return redirect('management')
    else:
        return redirect("cloud")


@login_required
def cloud(request):
    if request.user.is_superuser:
        return redirect('management')

    if not (UserProxy.has_any_data_center(request.user) or
            UserProxy.can_approve_workflow(request.user)):
        return redirect('no_udc')

    return render(request, "cloud.html")


#@superuser_required
def management(request):
    if request.user.is_superuser or request.user.has_perm("workflow.system_user") or request.user.has_perm("workflow.safety_user") or request.user.has_perm("workflow.audit_user"):
        return render(request, 'management.html',
                      {'inited': DataCenter.objects.exists()})
    else:
        Operation.log(request.user, obj_name="management", action="access_manage", result=0)
        return render(request, "cloud.html")


@login_required
def switch_idc(request, dc_id):
    try:
        dc = DataCenter.objects.get(pk=dc_id)

        udc_query = UDC.objects.filter(data_center=dc, user=request.user)

        if not udc_query.exists():
            link_user_to_dc_task(request.user, dc)

        request.session["UDC_ID"] = udc_query[0].id

    except Exception as ex:
        LOG.exception(ex)

    return HttpResponseRedirect(reverse("cloud"))


class LoginView(View):

    def get(self, request):
        return self.response(request)

    def post(self, request):
        form = AuthenticationForm(data=request.POST)

        if not form.is_valid():
            return self.response(request, form)

        user = form.get_user()
        auth_login(request, user)

        # Retrieve user to use some methods of UserProxy
        user = UserProxy.objects.get(pk=user.pk)
        if user.is_superuser or user.is_system_user or user.is_audit_user or user.is_safety_user:
            return redirect('management')

        udc_set = UDC.objects.filter(user=user)

        if udc_set.exists():
            request.session["UDC_ID"] = udc_set[0].id
        elif user.is_approver:
            request.session["UDC_ID"] = -1
        else:
            return redirect('no_udc')

        Notification.pull_announcements(user)

        return HttpResponseRedirect(reverse("cloud"))

    def response(self, request, form=None):

        if form is None:
            form = AuthenticationForm(initial={'username': ''})
            error = False
        else:
            error = True

        return render(request, 'login.html', {
            "form": form,
            "error": error
        })


def logout(request):
    auth_logout(request)
    return HttpResponseRedirect(reverse("index"))


class SignupView(View):

    def get(self, request):
        LOG.info("****** signup get method ********")
        datacenter = DataCenter.get_default()
        LOG.info("****** signup get method ********")
        rc = create_rc_by_dc(datacenter)
        LOG.info("****** signup get method ********")
        tenants = keystone.keystoneclient(rc).tenants.list()
        tenants_id = {}
        for tenant in tenants:
            if str(tenant.name) not in ["admin", "demo", "services"]:
                tenants_id[tenant.id] = tenant.name
        LOG.info("********* tenants_id is **************" + str(tenants_id))
        return self.response(request, CloudUserCreateForm(
            initial={'username': '',  'email': '', 'mobile': ''}), tenants_id)

    def post(self, request):

        user = User()
        form = CloudUserCreateForm(data=request.POST, instance=user)

        LOG.info("post start")
        tenant_id = request.POST.get("project")
        password = request.POST.get("password2")
        LOG.info("post start")
        if form.is_valid():
            form.save()
            LOG.info("1")
            if settings.REGISTER_ACTIVATE_EMAIL_ENABLED:
                LOG.info("2")
                _send_activate_email(user)
                msg = _("Your registration successed, we send you one "
                        "activate email, please check your input box.")
            else:
                LOG.info("start to run celery")
                link_user_to_dc_task(user, DataCenter.get_default(), tenant_id, password)
                LOG.info("end")
                msg = _("Your registration successed!")

            return render(request, 'info.html', {'message': msg})

        return self.response(request, form, form.errors)

    def response(self, request, form, tenants_id=None, errors=None):

        LOG.info("****** signup get method ********")
        datacenter = DataCenter.get_default()
        LOG.info("****** signup get method ********")
        rc = create_rc_by_dc(datacenter)
        LOG.info("****** signup get method ********")
        tenants = keystone.keystoneclient(rc).tenants.list()
        tenants_id = {}
        for tenant in tenants:
            if str(tenant.name) not in ["admin", "demo", "services"]:
                tenants_id[tenant.id] = tenant.name
        LOG.info("********* tenants_id is **************" + str(tenants_id))

        context = {
            "BRAND": settings.BRAND,
            "form": form,
            "errors": errors,
            "tenants_id": tenants_id
        }

        return render(request, 'signup.html', context)

    def dispatch(self, request, *args, **kwargs):

        if settings.REGISTER_ENABLED:
            LOG.info("dddd")
            return super(SignupView, self).dispatch(request, *args, **kwargs)
        else:
            return HttpResponseForbidden()


signup = SignupView.as_view()


def first_activate_user(request, code):

    try:
        activate_url = ActivateUrl.objects.get(code=code)
    except ActivateUrl.DoesNotExist:
        return _resend_email_response(request,
                                      _("This activate url is not valid, "
                                        "you can resend activate email."))

    if activate_url.expire_date < timezone.now():
        activate_url.delete()
        return _resend_email_response(request,
                                      _("This activate url is not valid, "
                                        "you can resend activate email."))

    try:
        link_user_to_dc_task(activate_url.user, DataCenter.get_default())
    except:
        return render(request, 'info.html', {
            'message': _("Failed to activate your account, you can try later.")
        })
    else:
        activate_url.delete()
        messages.add_message(request, messages.INFO,
                             _("Your account is activated successfully, "
                               "please login."))
        return redirect('login')


def resend_activate_email(request):

    username = request.POST['username']
    password = request.POST['password']

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return _resend_email_response(request, _("No account found."))

    if not user.check_password(password):
        return _resend_email_response(request, _("Password is not correct."))

    _send_activate_email(user)

    return render(request, 'info.html', {
        'message': _("Your activate email is sent, "
                     "please check your input box.")
    })


def _resend_email_response(request, error):
    return render(request, 'resend_activate_email.html', {'error': error})


def find_password(request):
    return render(request, 'find_password.html')


@login_required
def no_udc(request):

    login_via_ldap = hasattr(request.user, 'ldap_user')

    return render(request, 'no_udc.html', {
        "login_via_ldap": login_via_ldap
    })


def _send_activate_email(user):
    template = get_template('signup_activate_email.html')
    activate_url = ActivateUrl.generate(user)
    context = Context({
        'user': user,
        'url': activate_url.url,
        'BRAND': settings.BRAND,
        'EXTERNAL_URL': settings.EXTERNAL_URL})

    html = template.render(context)

    subject = _("%(site_name)s - Activate Account") \
        % {'site_name': settings.BRAND}

    send_mail.delay(subject, '',
                    user.email, html_message=html)


def not_found(request):
    if request.path.startswith('/api'):
        return JsonResponse({"success": False,
                             "msg": ugettext("Data not found!")},
                            status=404)

    return render(request, '404.html')


def server_error(request):
    if request.path.startswith('/api'):
        return JsonResponse({"success": False,
                             "msg": ugettext("System Error!")},
                            status=500)

    return render(request, '500.html')
