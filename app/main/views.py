from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.shortcuts import render_to_response, get_object_or_404
from django.template import Context, RequestContext, loader
from django.core.context_processors import csrf
from django.http import HttpResponseRedirect, Http404
from django.core.urlresolvers import reverse
from django.core.mail import send_mail
from django.utils import timezone
from django.contrib import messages

from .forms import (LoginForm, ProfileForm, RequestAccountForm, RequestPasswdForm,
                    ProcessAccountForm, ProcessPasswdForm, NewOrgForm)
from .models import Request

from webldap import settings
import ldapom
import re


def one(singleton):
    (e,) = singleton
    return e


def form(ctx, template, request):
    c = ctx
    c.update(csrf(request))
    return render_to_response(template, c, context_instance=RequestContext(request))


# Context processor
def session_info(request):
    return {'logged_in': request.session.get('ldap_connected', False),
            'logged_uid': request.session.get('ldap_binduid', None),
            'is_admin': request.session.get('is_admin', False)}


# View decorator
def connect_ldap(view, login_url='/login'):
    def _view(request, *args, **kwargs):
        if not request.session.get('ldap_connected', False):
            path = request.get_full_path()
            return HttpResponseRedirect('{}?next={}'.format(login_url, path))
        try:
            l = ldapom.LDAPConnection(uri=settings.LDAP_URI,
                                      base=settings.LDAP_BASE,
                                      bind_dn=request.session['ldap_binddn'],
                                      bind_password=request.session['ldap_passwd'])
        except (KeyError, ldapom.error.LDAPInvalidCredentialsError):
            messages.error(request, 'Identifiants incorrects.')
            return logout(request)

        # Login successful, check if admin
        if request.session.get('is_admin', None) is None:
            admins = l.get_entry('cn=admin,ou=roles,{}'.format(settings.LDAP_BASE)) \
                .roleOccupant
            request.session['is_admin'] = request.session['ldap_binddn'] in admins

        return view(request, l=l, *args, **kwargs)
    return _view


def error(request, error_msg):
    return render_to_response('main/error.html', {'error_msg': error_msg},
                              context_instance=RequestContext(request))


def login(request):
    redirect_to = request.GET.get('next', '/')

    if request.method == 'POST':
        f = LoginForm(request.POST)

        if f.is_valid():
            request.session.flush()
            request.session['ldap_connected'] = True
            request.session['ldap_binduid'] = f.cleaned_data['uid']
            request.session['ldap_binddn'] = 'uid={},ou=users,{}' \
                .format(f.cleaned_data['uid'], settings.LDAP_BASE)
            request.session['ldap_passwd'] = f.cleaned_data['passwd']
            return HttpResponseRedirect(redirect_to)
    else:
        f = LoginForm(label_suffix='')

    return form({'form': f}, 'main/login.html', request)


def logout(request, next=None):
    redirect_to = next or request.GET.get('next', '/')
    request.session.flush()

    return HttpResponseRedirect(redirect_to)


@connect_ldap
def profile(request, l):
    me = l.get_entry(request.session['ldap_binddn'])

    search = l.search('uniqueMember={}'.format(me.dn),
                      base='ou=associations,{}'.format(settings.LDAP_BASE))
    orgs = [{
        'uid': one(org.o),
        'name': one(org.cn),
        'is_owner': me.dn in org.owner
    } for org in search]

    search = list(l.search('uniqueMember={}'.format(me.dn),
                           base='ou=accesses,ou=groups,{}'.format(settings.LDAP_BASE)))
    search.extend(l.search('roleOccupant={}'.format(me.dn),
                           base='ou=roles,{}'.format(settings.LDAP_BASE)))

    groups = [{'name': one(group.cn), } for group in search]

    return render_to_response('main/profile.html', {
        'uid': me.uid,
        'name': me.displayName,
        'nick': one(me.cn),
        'email': one(me.mail),
        'orgs': orgs,
        'groups': groups,
    }, context_instance=RequestContext(request))


@sensitive_post_parameters('passwd')
@sensitive_variables('passwd_new')
@connect_ldap
def profile_edit(request, l):
    me = l.get_entry(request.session['ldap_binddn'])
    if request.method != 'POST':
        f = ProfileForm(label_suffix='', initial={'email': one(me.mail),
                                                  'name': me.displayName,
                                                  'nick': one(me.cn)})
        ctx = {'form': f, 'name': me.displayName, 'nick': one(me.cn), 'email': one(me.mail)}

        return form(ctx, 'main/edit.html', request)

    f = ProfileForm(request.POST)
    ctx = {'form': f, 'name': me.displayName, 'nick': one(me.cn), 'email': one(me.mail)}

    if not f.is_valid():
        return form(ctx, 'main/edit.html', request)

    name = f.cleaned_data['name']
    nick = f.cleaned_data['nick']

    if name != me.displayName or nick != one(me.cn):
        try:
            me.displayName = f.cleaned_data['name']
            me.cn = f.cleaned_data['nick']
            me.save()
        except ldapom.error.LDAPError:
            messages.error(request, 'Pseudo déjà pris ?')
            return form(ctx, 'main/edit.html', request)

    passwd_new = f.cleaned_data['passwd']
    email_new = f.cleaned_data['email']

    if passwd_new:
        try:
            me.set_password(passwd_new)
            request.session['ldap_passwd'] = passwd_new
        except ldapom.error.LDAPError:
            messages.error(request, 'Mot de passe trop court ?')
            return form(ctx, 'main/edit.html', request)

    if email_new != one(me.mail):
        req = Request()
        req.type = Request.EMAIL
        req.uid = one(me.uid)
        req.email = email_new
        req.save()

        t = loader.get_template('main/email_email_request')
        c = Context({'name': me.displayName,
                     'url': request.build_absolute_uri(
                         reverse(process, kwargs={'token': req.token})),
                     'expire_in': settings.REQ_EXPIRE_STR})
        send_mail('Confirmation email FedeRez', t.render(c), settings.EMAIL_FROM,
                  [req.email], fail_silently=False)
        messages.success(request, 'Un email vous a été envoyé pour confirmer'
                                  ' votre nouvelle adresse email')
        return HttpResponseRedirect('/')

    return form(ctx, 'main/edit.html', request)

@connect_ldap
def new_org(request, l):
    if not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes pas admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    if request.method == 'POST':
        req = NewOrgForm(request.POST)
        if req.is_valid():
            me = l.get_entry(request.session['ldap_binddn'])
            new_org = l.get_entry('o={},ou=associations,{}'.format(req.cleaned_data['name'], settings.LDAP_BASE))
            new_org.objectClass='groupOfUniqueNames'
            new_org.cn = req.cleaned_data['complete_name']
            new_org.uniqueMember.add(me.dn)
            new_org.save()
            messages.success(request, 'Association créée')
            return HttpResponseRedirect('/') 

    f = NewOrgForm()
    return form({'form': f}, 'main/new_org.html', request)

@connect_ldap
def org(request, l, uid):
    org = l.get_entry('o={},ou=associations,{}'.format(uid, settings.LDAP_BASE))
    admin = l.get_entry('cn=admin,ou=roles,{}'.format(settings.LDAP_BASE))
    ssh = l.get_entry('cn=ssh,ou=accesses,ou=groups,{}'.format(settings.LDAP_BASE))

    if not org.exists():
        raise Http404

    search = [l.get_entry(dn) for dn in org.uniqueMember]

    members = [{
        'uid': one(member.uid),
        'name': member.displayName,
        'owner': member.dn in org.owner,
        'is_admin': member.dn in admin.roleOccupant,
        'is_ssh': member.dn in ssh.uniqueMember,
    } for member in search]

    return render_to_response('main/org.html', {
        'uid': uid,
        'name': one(org.cn),
        'is_owner': request.session['ldap_binddn'] in org.owner,
        'members': members
    }, context_instance=RequestContext(request))


@connect_ldap
def org_promote(request, l, uid, user_uid):
    org = l.get_entry('o={},ou=associations,{}'.format(uid, settings.LDAP_BASE))
    user = l.get_entry('uid={},ou=users,{}'.format(user_uid, settings.LDAP_BASE))

    if not org.exists() or not user.exists():
        raise Http404

    if request.session['ldap_binddn'] not in org.owner \
            and not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes ni gérant, ni admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    org.owner.add(user.dn)
    org.save()

    messages.success(request, '{} est désormais gérant'.format(user.displayName))
    return HttpResponseRedirect('/org/{}'.format(uid))


@connect_ldap
def org_relegate(request, l, uid, user_uid):
    org = l.get_entry('o={},ou=associations,{}'.format(uid, settings.LDAP_BASE))
    user = l.get_entry('uid={},ou=users,{}'.format(user_uid, settings.LDAP_BASE))

    if not org.exists() or not user.exists():
        raise Http404

    if request.session['ldap_binddn'] not in org.owner \
            and not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes ni gérant, ni admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    org.owner.discard(user.dn)
    org.save()

    messages.success(request, '{} n\'est plus gérant'.format(user.displayName))
    return HttpResponseRedirect('/org/{}'.format(uid))


@connect_ldap
def org_add(request, l, uid):
    org = l.get_entry('o={},ou=associations,{}'.format(uid, settings.LDAP_BASE))
    if not org.exists():
        raise Http404

    if request.session['ldap_binddn'] not in org.owner \
            and not request.session['is_admin']:
        return error(request, 'Vous n\'êtes pas gérant.')

    if request.method == 'POST':
        f = RequestAccountForm(request.POST)
        if f.is_valid():
            req = f.save(commit=False)
            req.org_uid = uid
            req.type = Request.ACCOUNT
            req.save()

            t = loader.get_template('main/email_account_request')
            c = Context({
                'name': req.name,
                'url': request.build_absolute_uri(
                    reverse(process, kwargs={'token': req.token})),
                'expire_in': settings.REQ_EXPIRE_STR,
            })
            send_mail('Création de compte FedeRez', t.render(c), settings.EMAIL_FROM,
                      [req.email], fail_silently=False)
            messages.success(request,
                             'Email envoyé à {} pour la création du compte'
                             .format(req.email))

            return HttpResponseRedirect('/org/{}'.format(uid))
    else:
        f = RequestAccountForm(label_suffix='')

    return form({'form': f, 'name': one(org.cn), 'uid': uid}, 'main/org_add.html',
                request)


def make_posix(l, user):
    # Find free uid and gid for the user
    used_uid = [u.uidNumber for u in l.search('(uidNumber=*)')]
    used_gid = [u.gidNumber for u in l.search('(gidNumber=*)')]

    uid_number = 10000
    while uid_number in used_uid or uid_number in used_gid:
        uid_number += 1

    federez_uid = re.sub(r'[^a-zA-Z]+', '', one(user.cn))

    # Ensure the netFederezUID has not already been taken
    if not list(l.search('netFederezUID={}'.format(federez_uid))) == []:
        return 'Le pseudo est déjà utilisé, impossible d\'ajouter un accès ssh'

    user.objectClass.add('shadowAccount')
    user.objectClass.add('netFederezUser')
    user.objectClass.add('posixAccount')
    user.gidNumber = uid_number
    user.homeDirectory = '/home/' + one(user.cn)
    user.loginShell = '/bin/bash'
    user.netFederezUID = federez_uid
    user.shadowMax = 99999
    user.shadowMin = 0
    user.shadowWarning = 7
    user.uidNumber = uid_number
    user.save()

    group = l.get_entry('cn={},ou=posix,ou=groups,{}'
                        .format(one(user.netFederezUID), settings.LDAP_BASE))
    group.objectClass = 'posixGroup'
    group.cn = one(user.netFederezUID)
    group.gidNumber = uid_number
    group.memberUid = one(user.netFederezUID)
    group.save()


@connect_ldap
def enable_ssh(request, l, uid, user_uid):
    ssh = l.get_entry('cn=ssh,ou=accesses,ou=groups,{}'.format(settings.LDAP_BASE))
    user = l.get_entry('uid={},ou=users,{}'.format(user_uid, settings.LDAP_BASE))

    if not user.exists():
        raise Http404

    if not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes pas admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    # Ensure the user has POSIX and netFederezUser attributes
    if user.dn not in [us.dn for us in l.search('(objectClass=netFederezUser)')]:
        posix = make_posix(l, user)
        if posix is not None:
            messages.error(request, posix)
            return HttpResponseRedirect('/org/{}'.format(uid))

    ssh.uniqueMember.add(user.dn)
    ssh.save()

    messages.success(request, '{} a désormais des accès SSH'.format(user.displayName))
    return HttpResponseRedirect('/org/{}'.format(uid))


@connect_ldap
def disable_ssh(request, l, uid, user_uid):
    user = l.get_entry('uid={},ou=users,{}'.format(user_uid, settings.LDAP_BASE))
    ssh = l.get_entry('cn=ssh,ou=accesses,ou=groups,{}'.format(settings.LDAP_BASE))

    if not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes pas admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    ssh.uniqueMember.discard(user.dn)
    ssh.save()

    messages.success(request, '{} n\'a plus d\'accès SSH'.format(user.displayName))
    return HttpResponseRedirect('/org/{}'.format(uid))


@connect_ldap
def enable_admin(request, l, uid, user_uid):
    user = l.get_entry('uid={},ou=users,{}'.format(user_uid, settings.LDAP_BASE))
    sudo_ssh = l.get_entry('cn=sudoldap,ou=posix,ou=groups,{}'.format(settings.LDAP_BASE))
    admin = l.get_entry('cn=admin,ou=roles,{}'.format(settings.LDAP_BASE))

    if not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes pas admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    if user.dn not in [us.dn for us in l.search('(objectClass=netFederezUser)')]:
        messages.error(request, 'Il faut ajouter le membre au groupe ssh avant')
        return HttpResponseRedirect('/org/{}'.format(uid))

    sudo_ssh.memberUid.add(one(user.netFederezUID))
    sudo_ssh.save()

    admin.roleOccupant.add(user.dn)
    admin.save()

    messages.success(request,
                     '{} est désormais admin et a des accès sudo sur les serveurs'
                     .format(user.displayName))
    return HttpResponseRedirect('/org/{}'.format(uid))


@connect_ldap
def disable_admin(request, l, uid, user_uid):
    user = l.get_entry('uid={},ou=users,{}'.format(user_uid, settings.LDAP_BASE))
    sudo_ssh = l.get_entry('cn=sudoldap,ou=posix,ou=groups,{}'.format(settings.LDAP_BASE))
    admin = l.get_entry('cn=admin,ou=roles,{}'.format(settings.LDAP_BASE))

    if not request.session['is_admin']:
        messages.error(request, 'Vous n\'êtes pas admin')
        return HttpResponseRedirect('/org/{}'.format(uid))

    sudo_ssh.memberUid.discard(one(user.netFederezUID))
    sudo_ssh.save()

    admin.roleOccupant.discard(user.dn)
    admin.save()

    messages.success(request,
                     '{} n\'est plus admin et ses accès ssh sudo ont été révoqués'
                     .format(user.displayName))
    return HttpResponseRedirect('/org/{}'.format(uid))


@connect_ldap
def admin(request, l):
    me = l.get_entry(request.session['ldap_binddn'])

    if not request.session['is_admin']:
        return error(request, 'Vous n\'êtes pas administrateur')

    search = l.search('(objectClass=groupOfUniqueNames)',
                      base='ou=associations,{}'.format(settings.LDAP_BASE))
    orgs = [{
        'uid': one(org.o),
        'name': one(org.cn),
        'is_owner': me.dn in org.owner,
    } for org in search]

    return render_to_response('main/admin.html', {'orgs': orgs},
                              context_instance=RequestContext(request))


def passwd(request):
    if request.method == 'POST':
        f = RequestPasswdForm(request.POST)
        if f.is_valid():
            req = f.save(commit=False)
            l = ldapom.LDAPConnection(uri=settings.LDAP_URI,
                                      base=settings.LDAP_BASE,
                                      bind_dn=settings.LDAP_WEBLDAP_USER,
                                      bind_password=settings.LDAP_WEBLDAP_PASSWD)
            try:
                user = list(l.search('(&(uid={})(mail={}))'.format(req.uid, req.email),
                                     base='ou=users,{}'.format(settings.LDAP_BASE)))[0]
            except IndexError:
                messages.error(request, 'Données incorrectes')
            else:
                req.type = Request.PASSWD
                req.save()

                t = loader.get_template('main/email_passwd_request')
                c = Context({
                    'name': user.displayName,
                    'url': request.build_absolute_uri(
                        reverse(process, kwargs={'token': req.token})),
                    'expire_in': settings.REQ_EXPIRE_STR,
                })
                send_mail('Changement de mot de passe FedeRez', t.render(c),
                          settings.EMAIL_FROM, [one(user.mail)], fail_silently=False)
                return HttpResponseRedirect('/')
    else:
        f = RequestPasswdForm(label_suffix='')

    return form({'form': f}, 'main/passwd.html', request)


def process(request, token):
    valid_reqs = Request.objects.filter(expires_at__gt=timezone.now())
    req = get_object_or_404(valid_reqs, token=token)

    if req.type == Request.ACCOUNT:
        return process_account(request, req)
    elif req.type == Request.PASSWD:
        return process_passwd(request, req)
    elif req.type == Request.EMAIL:
        return process_email(request, req=req)
    else:
        return error(request, 'Entrée incorrecte, contactez un admin')


def process_account(request, req):
    if request.method != 'POST':
        f = ProcessAccountForm(label_suffix='')
        return form({'form': f}, 'main/process_account.html', request)

    f = ProcessAccountForm(request.POST)
    if not f.is_valid():
        return form({'form': f}, 'main/process_account.html', request)

    l = ldapom.LDAPConnection(uri=settings.LDAP_URI,
                              base=settings.LDAP_BASE,
                              bind_dn=settings.LDAP_WEBLDAP_USER,
                              bind_password=settings.LDAP_WEBLDAP_PASSWD)
    user = l.get_entry('uid={},ou=users,{}'.format(req.uid, settings.LDAP_BASE))

    if user.exists():
        messages.error(request, 'Compte déjà créé')
        return HttpResponseRedirect('/')

    user.objectClass = 'inetOrgPerson'
    user.uid = req.uid
    user.displayName = req.name
    user.mail = req.email
    user.cn = f.cleaned_data['nick']
    user.sn = 'CHANGEIT!'  # TODO

    try:
        user.save()
    except ldapom.error.LDAPError:
        messages.error(request, 'Pseudo déjà pris ?')
        return form({'form': f}, 'main/process_account.html', request)

    try:
        user.set_password(f.cleaned_data['passwd'])
    except ldapom.error.LDAPError:
        user.delete()
        messages.error(request, 'Mot de passe trop court ?')
        return form({'form': f}, 'main/process_account.html', request)

    if req.org_uid:
        org = l.get_entry('o={},ou=associations,{}'
                          .format(req.org_uid, settings.LDAP_BASE))
        org.uniqueMember.add(user.dn)
        org.save()

    for group in settings.LDAP_DEFAULT_GROUPS:
        group = l.get_entry('cn={},ou=accesses,ou=groups,{}'
                            .format(group, settings.LDAP_BASE))
        group.uniqueMember.add(user.dn)
        group.save()

    for role in settings.LDAP_DEFAULT_ROLES:
        role = l.get_entry('cn={},ou=roles,{}'
                           .format(role, settings.LDAP_BASE))
        role.roleOccupant.add(user.dn)
        role.save()

    req.delete()
    messages.success(request, 'Compte créé')
    return HttpResponseRedirect('/')


@sensitive_post_parameters()
def process_passwd(request, req):
    if request.method == 'POST':
        f = ProcessPasswdForm(request.POST)
        if f.is_valid():
            l = ldapom.LDAPConnection(uri=settings.LDAP_URI,
                                      base=settings.LDAP_BASE,
                                      bind_dn=settings.LDAP_WEBLDAP_USER,
                                      bind_password=settings.LDAP_WEBLDAP_PASSWD)
            user = l.get_entry('uid={},ou=users,{}'.format(req.uid, settings.LDAP_BASE))

            try:
                user.set_password(f.cleaned_data['passwd'])
            except ldapom.error.LDAPError:
                messages.error(request, 'Mot de passe trop court ?')
                return form({'form': f}, 'main/process_passwd.html', request)

            req.delete()
            messages.success(request, 'Mot de passe changé')

            return HttpResponseRedirect('/')
    else:
        f = ProcessPasswdForm(label_suffix='')

    return form({'form': f}, 'main/process_passwd.html', request)


@connect_ldap
def process_email(request, l, req):
    request_dn = 'uid={},ou=users,{}'.format(req.uid, settings.LDAP_BASE)

    # User who requested email change must be logged in
    if request.session['ldap_binddn'] != request_dn:
        return logout(request, next=reverse(process, kwargs={'token': req.token}))

    user = l.get_entry(request.session['ldap_binddn'])
    user.mail = req.email
    user.save()
    req.delete()
    messages.success(request, 'Email confirmé')

    return HttpResponseRedirect('/')


def help(request):
    return render_to_response('main/help.html', context_instance=RequestContext(request))
