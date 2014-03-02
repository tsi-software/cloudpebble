from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseBadRequest
from django.utils import simplejson as json
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.views.decorators.http import require_safe, require_POST
from django.views.decorators.csrf import csrf_protect, csrf_exempt, ensure_csrf_cookie
from django.conf import settings
from celery.result import AsyncResult

from ide.models import Project, SourceFile, ResourceFile, ResourceIdentifier, BuildResult, TemplateProject, UserGithub, generate_half_uuid
from ide.tasks import run_compile, create_archive, do_import_archive, do_import_github, do_github_push, do_github_pull, hooked_commit, export_user_projects
from ide.forms import SettingsForm
import ide.git
from utils.keen_helper import send_keen_event
from utils.redis_helper import redis_client

import urllib2
import urllib
import base64
import tempfile
import os
import re
import uuid
import datetime
import time
import requests
from github import UnknownObjectException

def json_response(response=None):
    if response is None:
        response = {}

    response["success"] = True
    return HttpResponse(json.dumps(response), content_type="application/json")

def json_failure(error):
    return HttpResponse(json.dumps({"success": False, "error": error}), content_type="application/json")

@require_safe
@login_required
@ensure_csrf_cookie
def index(request):
    my_projects = Project.objects.filter(owner=request.user).order_by('-last_modified')
    if not request.user.settings.accepted_terms:
        return render(request, 'ide/new-owner.html', {
            'my_projects': my_projects
        })
    elif settings.SOCIAL_AUTH_PEBBLE_REQUIRED and request.user.social_auth.filter(provider='pebble').count() == 0:
        return render(request, 'registration/merge_account.html')
    else:
        send_keen_event('cloudpebble', 'cloudpebble_project_list', request=request)
        return render(request, 'ide/index.html', {
            'my_projects': my_projects,
            'sdk_templates': TemplateProject.objects.filter(template_kind=TemplateProject.KIND_TEMPLATE),
            'example_templates': TemplateProject.objects.filter(template_kind=TemplateProject.KIND_EXAMPLE),
            'demo_templates': TemplateProject.objects.filter(template_kind=TemplateProject.KIND_SDK_DEMO),
            'default_template_id': settings.DEFAULT_TEMPLATE
        })


@require_safe
@login_required
@ensure_csrf_cookie
def project(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    if project.app_uuid is None:
        project.app_uuid = generate_half_uuid()
    if project.app_company_name is None:
        project.app_company_name = request.user.username
    if project.app_short_name is None:
        project.app_short_name = project.name
    if project.app_long_name is None:
        project.app_long_name = project.app_short_name
    if project.app_version_code is None:
        project.app_version_code = 1
    if project.app_version_label is None:
        project.app_version_label = '1.0'
    send_keen_event('cloudpebble', 'cloudpebble_open_project', request=request, project=project)
    return render(request, 'ide/project.html', {'project': project})


@require_safe
@login_required
def create(request):
    return render(request, 'ide/create.html')


@require_safe
@login_required
def project_info(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    source_files = SourceFile.objects.filter(project=project)
    resources = ResourceFile.objects.filter(project=project)
    output = {
        'success': True,
        'name': project.name,
        'last_modified': str(project.last_modified),
        'version_def_name': project.version_def_name,
        'app_uuid': project.app_uuid or '',
        'app_company_name': project.app_company_name,
        'app_short_name': project.app_short_name,
        'app_long_name': project.app_long_name,
        'app_version_code': project.app_version_code,
        'app_version_label': project.app_version_label,
        'app_is_watchface': project.app_is_watchface,
        'app_capabilities': project.app_capabilities,
        'app_jshint': project.app_jshint,
        'menu_icon': project.menu_icon.id if project.menu_icon else None,
        'sdk_version': project.sdk_version,
        'source_files': [{'name': f.file_name, 'id': f.id} for f in source_files],
        'resources': [{
            'id': x.id,
            'file_name': x.file_name,
            'kind': x.kind,
            'identifiers': [y.resource_id for y in x.identifiers.all()],
        } for x in resources],
        'github': {
            'repo': "github.com/%s" % project.github_repo if project.github_repo is not None else None,
            'branch': project.github_branch if project.github_branch is not None else None,
            'last_sync': str(project.github_last_sync) if project.github_last_sync is not None else None,
            'last_commit': project.github_last_commit,
            'auto_build': project.github_hook_build,
            'auto_pull': project.github_hook_uuid is not None
        }
    }

    return json_response(output)


@require_POST
@login_required
def create_source_file(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    try:
        f = SourceFile.objects.create(project=project, file_name=request.POST['name'])
    except IntegrityError as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_create_file', data={
            'data': {
                'filename': request.POST['name'],
                'kind': 'source'
            }
        }, project=project, request=request)

        return json_response({"file": {"id": f.id, "name": f.file_name}})


@require_safe
@csrf_protect
@login_required
def load_source_file(request, project_id, file_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    source_file = get_object_or_404(SourceFile, pk=file_id, project=project)
    try:
        content = source_file.get_contents()

        send_keen_event('cloudpebble', 'cloudpebble_open_file', data={
            'data': {
                'filename': source_file.file_name,
                'kind': 'source'
            }
        }, project=project, request=request)

    except Exception as e:
        return json_failure(str(e))
    else:
        return json_response({
            "success": True,
            "source": content,
            "modified": time.mktime(source_file.last_modified.utctimetuple())
        })

@require_safe
@csrf_protect
@login_required
def source_file_is_safe(request, project_id, file_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    source_file = get_object_or_404(SourceFile, pk=file_id, project=project)
    client_modified = datetime.datetime.fromtimestamp(int(request.GET['modified']))
    server_modified = source_file.last_modified.replace(tzinfo=None, microsecond=0)
    is_safe = client_modified >= server_modified
    return json_response({'safe': is_safe})


@require_POST
@login_required
def save_source_file(request, project_id, file_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    source_file = get_object_or_404(SourceFile, pk=file_id, project=project)
    try:
        expected_modification_time = datetime.datetime.fromtimestamp(int(request.POST['modified']))
        if source_file.last_modified.replace(tzinfo=None, microsecond=0) > expected_modification_time:
            send_keen_event('cloudpebble', 'cloudpebble_save_abort_unsafe', data={
                'data': {
                    'filename': source_file.file_name,
                    'kind': 'source'
                }
            }, project=project, request=request)
            raise Exception("Could not save: file has been modified since last save.")
        source_file.save_file(request.POST['content'])


    except Exception as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_save_file', data={
            'data': {
                'filename': source_file.file_name,
                'kind': 'source'
            }
        }, project=project, request=request)

        return json_response({"modified": time.mktime(source_file.last_modified.utctimetuple())})


@require_POST
@login_required
def delete_source_file(request, project_id, file_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    source_file = get_object_or_404(SourceFile, pk=file_id, project=project)
    try:
        source_file.delete()
    except Exception as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_delete_file', data={
            'data': {
                'filename': source_file.file_name,
                'kind': 'source'
            }
        }, project=project, request=request)
        return json_response({})


@require_POST
@login_required
def create_resource(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    kind = request.POST['kind']
    resource_ids = json.loads(request.POST['resource_ids'])
    file_name = request.FILES['file'].name
    resources = []
    try:
        with transaction.commit_on_success():
            rf = ResourceFile.objects.create(project=project, file_name=file_name, kind=kind)
            for r in resource_ids:
                regex = r['regex'] if 'regex' in r else None
                tracking = int(r['tracking']) if 'tracking' in r else None
                resources.append(ResourceIdentifier.objects.create(resource_file=rf, resource_id=r['id'], character_regex=regex, tracking=tracking))
            rf.save_file(request.FILES['file'])


    except Exception as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_create_file', data={
            'data': {
                'filename': file_name,
                'kind': 'resource',
                'resource-kind': kind
            }
        }, project=project, request=request)

        return json_response({"file": {
            "id": rf.id,
            "kind": rf.kind,
            "file_name": rf.file_name,
            "resource_ids": [{'id': x.resource_id, 'regex': x.character_regex} for x in resources],
            "identifiers": [x.resource_id for x in resources]
        }})


@require_safe
@login_required
def resource_info(request, project_id, resource_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    resource = get_object_or_404(ResourceFile, pk=resource_id)
    resources = resource.get_identifiers()

    send_keen_event('cloudpebble', 'cloudpebble_open_file', data={
        'data': {
            'filename': resource.file_name,
            'kind': 'resource',
            'resource-kind': resource.kind
        }
    }, project=project, request=request)

    return json_response({
        'resource': {
            'resource_ids': [{'id': x.resource_id, 'regex': x.character_regex, 'tracking': x.tracking} for x in resources],
            'id': resource.id,
            'file_name': resource.file_name,
            'kind': resource.kind
        }
    })


@require_POST
@login_required
def delete_resource(request, project_id, resource_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    resource = get_object_or_404(ResourceFile, pk=resource_id, project=project)
    try:
        resource.delete()
    except Exception as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_delete_file', data={
            'data': {
                'filename': resource.file_name,
                'kind': 'resource',
                'resource-kind': resource.kind
            }
        }, project=project, request=request)

        return json_response({})


@require_POST
@login_required
def update_resource(request, project_id, resource_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    resource = get_object_or_404(ResourceFile, pk=resource_id, project=project)
    resource_ids = json.loads(request.POST['resource_ids'])
    try:
        with transaction.commit_on_success():
            # Lazy approach: delete all the resource_ids and recreate them.
            # We could do better.
            resources = []
            ResourceIdentifier.objects.filter(resource_file=resource).delete()
            for r in resource_ids:
                regex = r['regex'] if 'regex' in r else None
                tracking = int(r['tracking']) if 'tracking' in r else None
                resources.append(ResourceIdentifier.objects.create(resource_file=resource, resource_id=r['id'], character_regex=regex, tracking=tracking))

            if 'file' in request.FILES:
                resource.save_file(request.FILES['file'])
    except Exception as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_save_file', data={
            'data': {
                'filename': resource.file_name,
                'kind': 'source'
            }
        }, project=project, request=request)

        return json_response({"file": {
            "id": resource.id,
            "kind": resource.kind,
            "file_name": resource.file_name,
            "resource_ids": [{'id': x.resource_id, 'regex': x.character_regex} for x in resources],
            "identifiers": [x.resource_id for x in resources]
        }})


@require_POST
@login_required
def compile_project(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    build = BuildResult.objects.create(project=project)
    task = run_compile.delay(build.id)
    return json_response({"build_id": build.id, "task_id": task.task_id})


@require_safe
@login_required
def last_build(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    try:
        build = project.builds.order_by('-started')[0]
    except (IndexError, BuildResult.DoesNotExist) as e:
        return json_response({"build": None})
    else:
        b = {
            'uuid': build.uuid,
            'state': build.state,
            'started': str(build.started),
            'finished': str(build.finished) if build.finished else None,
            'id': build.id,
            'pbw': build.pbw_url,
            'log': build.build_log_url,
            'size': {
                'total': build.total_size,
                'binary': build.binary_size,
                'resources': build.resource_size
            }
        }
        return json_response({"build": b})


@require_safe
@login_required
def build_history(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    try:
        builds = project.builds.order_by('-started')[:10]
    except (IndexError, BuildResult.DoesNotExist):
        return json_response({"build": None})
    else:
        out = []
        for build in builds:
            out.append({
                'uuid': build.uuid,
                'state': build.state,
                'started': str(build.started),
                'finished': str(build.finished) if build.finished else None,
                'id': build.id,
                'pbw': build.pbw_url,
                'log': build.build_log_url,
                'debug': build.debug_info_url,
                'size': {
                    'total': build.total_size,
                    'binary': build.binary_size,
                    'resources': build.resource_size
                }
            })
        return json_response({"builds": out})


@require_safe
@login_required
def build_log(request, project_id, build_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    build = get_object_or_404(BuildResult, project=project, pk=build_id)
    try:
        log = open(build.build_log, 'r').read().decode('utf-8')
    except Exception as e:
        return json_failure(str(e))

    send_keen_event('cloudpebble', 'cloudpebble_view_build_log', data={
        'data': {
            'build_state': build.state
        }
    }, project=project, request=request)

    return json_response({"log": log})


@require_POST
@login_required
def create_project(request):
    name = request.POST['name']
    template_id = request.POST.get('template', None)
    try:
        with transaction.commit_on_success():
            project = Project.objects.create(
                name=name,
                owner=request.user,
                sdk_version=2,
                app_company_name=request.user.username,
                app_short_name=name,
                app_long_name=name,
                app_version_code=1,
                app_version_label='1.0',
                app_is_watchface=False,
                app_capabilities=''
            )
            if template_id is not None and int(template_id) != 0:
                template = TemplateProject.objects.get(pk=int(template_id))
                template.copy_into_project(project)
    except IntegrityError as e:
        return json_failure(str(e))
    else:

        send_keen_event('cloudpebble', 'cloudpebble_create_project', project=project, request=request)

        return json_response({"id": project.id})


@require_POST
@login_required
def save_project_settings(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    try:
        with transaction.commit_on_success():
            sdk_version = int(request.POST['sdk_version'])
            project.name = request.POST['name']
            project.sdk_version = sdk_version
            if sdk_version == 1:
                project.version_def_name = request.POST['version_def_name']
            elif sdk_version > 1:
                project.app_uuid = request.POST['app_uuid']
                project.app_company_name = request.POST['app_company_name']
                project.app_short_name = request.POST['app_short_name']
                project.app_long_name = request.POST['app_long_name']
                project.app_version_code = int(request.POST['app_version_code'])
                project.app_version_label = request.POST['app_version_label']
                project.app_is_watchface = bool(int(request.POST['app_is_watchface']))
                project.app_capabilities = request.POST['app_capabilities']
                project.app_keys = request.POST['app_keys']
                project.app_jshint = bool(int(request.POST['app_jshint']))

                menu_icon = request.POST['menu_icon']
                if menu_icon != '':
                    menu_icon = int(menu_icon)
                    old_icon = project.menu_icon
                    if old_icon is not None:
                        old_icon.is_menu_icon = False
                        old_icon.save()
                    icon_resource = project.resources.filter(id=menu_icon)[0]
                    icon_resource.is_menu_icon = True
                    icon_resource.save()

            project.save()
    except IntegrityError as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_save_project_settings', project=project, request=request)

        return json_response({})


@require_POST
@login_required
def delete_project(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    if not bool(request.POST.get('confirm', False)):
        return json_failure("Not confirmed")
    try:
        project.delete()
    except Exception as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_delete_project', project=project, request=request)
        return json_response({})


@require_safe
@login_required
def show_resource(request, project_id, resource_id):
    resource = get_object_or_404(ResourceFile, pk=resource_id, project__owner=request.user)
    content_type = {'png': 'image/png', 'png-trans': 'image/png', 'font': 'application/octet-stream', 'raw': 'application/octet-stream'}
    response = HttpResponse(open(resource.local_filename), content_type=content_type[resource.kind])
    response['Content-Disposition'] = "attachment; filename=\"%s\"" % resource.file_name
    return response


@require_POST
def get_shortlink(request):
    url = request.POST['url']
    try:
        r = urllib2.Request('http://api.small.cat/entries', json.dumps({'value': url, 'duration': 60}), headers={'Content-Type': 'application/json'})
        response = json.loads(urllib2.urlopen(r).read())
    except urllib2.URLError as e:
        return json_failure(str(e))
    else:
        send_keen_event('cloudpebble', 'cloudpebble_generate_shortlink', data={
            'data': {'short_url': response['url']}
        }, request=request)
        return json_response({'url': response['url']})


@login_required
def settings_page(request):
    settings = request.user.settings
    try:
        github = request.user.github
    except UserGithub.DoesNotExist:
        github = None

    if request.method == 'POST':
        form = SettingsForm(request.POST, instance=settings)
        if form.is_valid():
            form.save()
            send_keen_event('cloudpebble', 'cloudpebble_change_user_settings', request=request)
            return render(request, 'ide/settings.html', {'form': form, 'saved': True, 'github': github})

    else:
        form = SettingsForm(instance=settings)

    send_keen_event('cloudpebble', 'cloudpebble_view_user_settings', request=request)

    return render(request, 'ide/settings.html', {'form': form, 'saved': False, 'github': github})


@login_required
@require_POST
def begin_export(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    result = create_archive.delay(project.id)
    return json_response({'task_id': result.task_id})


@require_safe
def check_task(request, task_id):
    result = AsyncResult(task_id)
    return json_response({
        'state': {
            'status': result.status,
            'result': result.result if result.status == 'SUCCESS' else str(result.result)
        }
    })


@login_required
@require_POST
def import_zip(request):
    zip_file = request.FILES['archive']
    fd, tempzip = tempfile.mkstemp(suffix='.zip')
    f = os.fdopen(fd, 'w')
    for chunk in zip_file.chunks():
        f.write(chunk)
    name = request.POST['name']
    try:
        project = Project.objects.create(owner=request.user, name=name)
    except IntegrityError as e:
        return json_failure(str(e))
    task = do_import_archive.delay(project.id, tempzip, delete_zip=True, delete_project=True)

    return json_response({'task_id': task.task_id, 'project_id': project.id})


@login_required
@require_POST
def import_github(request):
    name = request.POST['name']
    repo = request.POST['repo']
    branch = request.POST['branch']
    match = re.match(r'^(?:https?://|git@|git://)?(?:www\.)?github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git|/|$)', repo)
    if match is None:
        return HttpResponse(json.dumps({"success": False, 'error': "Invalid GitHub URL."}), content_type="application/json")
    github_user = match.group(1)
    github_project = match.group(2)

    try:
        project = Project.objects.create(owner=request.user, name=name)
    except IntegrityError as e:
        return json_failure(str(e))

    task = do_import_github.delay(project.id, github_user, github_project, branch, delete_project=True)
    return json_response({'task_id': task.task_id, 'project_id': project.id})


@login_required
@require_safe
def start_github_auth(request):
    nonce = uuid.uuid4().hex
    try:
        user_github = request.user.github
    except UserGithub.DoesNotExist:
        user_github = UserGithub.objects.create(user=request.user)
    user_github.nonce = nonce
    user_github.save()
    send_keen_event('cloudpebble', 'cloudpebble_github_started', request=request)
    return HttpResponseRedirect('https://github.com/login/oauth/authorize?client_id=%s&scope=repo&state=%s' % (settings.GITHUB_CLIENT_ID, nonce))


@login_required
@require_POST
def remove_github_auth(request):
    try:
        user_github = request.user.github
        user_github.delete()
    except UserGithub.DoesNotExist:
        pass
    send_keen_event('cloudpebble', 'cloudpebble_github_revoked', request=request)
    return HttpResponseRedirect('/ide/settings')


@login_required
@require_safe
def complete_github_auth(request):
    if 'error' in request.GET:
        return HttpResponseRedirect('/ide/settings')
    nonce = request.GET['state']
    code = request.GET['code']
    user_github = request.user.github
    if user_github.nonce is None or nonce != user_github.nonce:
        return HttpResponseBadRequest('nonce mismatch.')
    # This probably shouldn't be in a view. Oh well.
    params = urllib.urlencode({'client_id': settings.GITHUB_CLIENT_ID, 'client_secret': settings.GITHUB_CLIENT_SECRET, 'code': code})
    r = urllib2.Request('https://github.com/login/oauth/access_token', params, headers={'Accept': 'application/json'})
    result = json.loads(urllib2.urlopen(r).read())
    user_github = request.user.github
    user_github.token = result['access_token']
    user_github.nonce = None
    # Try and figure out their username.
    auth_string = base64.encodestring('%s:%s' % (settings.GITHUB_CLIENT_ID, settings.GITHUB_CLIENT_SECRET)).replace('\n', '')
    r = urllib2.Request('https://api.github.com/applications/%s/tokens/%s' % (settings.GITHUB_CLIENT_ID, user_github.token))
    r.add_header("Authorization", "Basic %s" % auth_string)
    result = json.loads(urllib2.urlopen(r).read())
    user_github.username = result['user']['login']
    user_github.avatar = result['user']['avatar_url']

    user_github.save()

    send_keen_event('cloudpebble', 'cloudpebble_github_linked', request=request, data={'data': {'username': user_github.username}})

    return HttpResponseRedirect('/ide/settings')


@login_required
@require_POST
def github_push(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    commit_message = request.POST['commit_message']
    task = do_github_push.delay(project.id, commit_message)
    return json_response({'task_id': task.task_id})


@login_required
@require_POST
def github_pull(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    task = do_github_pull.delay(project.id)
    return json_response({'task_id': task.task_id})


@login_required
@require_POST
def set_project_repo(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    repo = request.POST['repo']
    branch = request.POST['branch']
    auto_pull = bool(int(request.POST['auto_pull']))
    auto_build = bool(int(request.POST['auto_build']))

    repo = ide.git.url_to_repo(repo)
    if repo is None:
        return json_failure("Invalid repo URL.")
    repo = '%s/%s' % repo

    g = ide.git.get_github(request.user)
    try:
        g_repo = g.get_repo(repo)
    except UnknownObjectException:
        return json_response({'exists': False, 'access': False, 'updated': False, 'branch_exists': False})

    # TODO: Validate the branch...give user option to create one?

    with transaction.commit_on_success():
        if repo != project.github_repo:
            if project.github_hook_uuid:
                try:
                    remove_hooks(g.get_repo(project.github_repo), project.github_hook_uuid)
                except:
                    pass

            # Just clear the repo if none specified.
            if repo == '':
                project.github_repo = None
                project.github_branch = None
                project.github_last_sync = None
                project.github_last_commit = None
                project.github_hook_uuid = None
                project.save()
                return json_response({'exists': True, 'access': True, 'updated': True, 'branch_exists': True})

            if not ide.git.git_verify_tokens(request.user):
                return json_failure("No GitHub tokens on file.")

            try:
                has_access = ide.git.check_repo_access(request.user, repo)
            except UnknownObjectException:
                return json_response({'exists': False, 'access': False, 'updated': False, 'branch_exists': False})

            if has_access:
                project.github_repo = repo
                project.github_branch = branch
                project.github_last_sync = None
                project.github_last_commit = None
                project.github_hook_uuid = None
            else:
                return json_response({'exists': True, 'access': True, 'updated': True, 'branch_exists': True})

        if branch != project.github_branch:
            project.github_branch = branch

        if auto_pull and project.github_hook_uuid is None:
            # Generate a new hook UUID
            project.github_hook_uuid = uuid.uuid4().hex
            # Set it up
            try:
                g_repo.create_hook('web', {'url': settings.GITHUB_HOOK_TEMPLATE % {'project': project.id, 'key': project.github_hook_uuid}, 'content_type': 'form'}, ['push'], True)
            except Exception as e:
                return json_failure(str(e))
        elif not auto_pull:
            if project.github_hook_uuid is not None:
                try:
                    remove_hooks(g_repo, project.github_hook_uuid)
                except:
                    pass
                project.github_hook_uuid = None

        project.github_hook_build = auto_build

        project.save()

    send_keen_event('cloudpebble', 'cloudpebble_project_github_linked', project=project, request=request, data={
        'data': {
            'repo': project.github_repo,
            'branch': project.github_branch
        }
    })

    return json_response({'exists': True, 'access': True, 'updated': True, 'branch_exists': True})


@login_required
@require_POST
def create_project_repo(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    repo = request.POST['repo']
    description = request.POST['description']
    try:
        repo = ide.git.create_repo(request.user, repo, description)
    except Exception as e:
        return json_failure(str(e))
    else:
        project.github_repo = repo.full_name
        project.github_branch = "master"
        project.github_last_sync = None
        project.github_last_commit = None
        project.save()

    send_keen_event('cloudpebble', 'cloudpebble_created_github_repo', project=project, request=request, data={
        'data': {
            'repo': project.github_repo
        }
    })

    return json_response({"repo": repo.html_url})


def remove_hooks(repo, s):
    hooks = list(repo.get_hooks())
    for hook in hooks:
        if hook.name != 'web':
            continue
        if s in hook.config['url']:
            hook.delete()


@csrf_exempt
@require_POST
def github_hook(request, project_id):
    hook_uuid = request.GET['key']
    project = get_object_or_404(Project, pk=project_id, github_hook_uuid=hook_uuid)

    push_info = json.loads(request.POST['payload'])
    if push_info['ref'] == 'refs/heads/%s' % (project.github_branch or push_info['repository']['master_branch']):
        hooked_commit.delay(project_id, push_info['after'])

    return HttpResponse('ok')

@require_safe
def build_status(request, project_id):
    project = get_object_or_404(Project, pk=project_id)
    try:
        last_build = BuildResult.objects.order_by('-id').filter(~Q(state=BuildResult.STATE_WAITING), project=project)[0]
    except IndexError:
        return HttpResponseRedirect(settings.STATIC_URL + '/ide/img/status/error.png')
    if last_build.state == BuildResult.STATE_SUCCEEDED:
        return HttpResponseRedirect(settings.STATIC_URL + '/ide/img/status/passing.png')
    else:
        return HttpResponseRedirect(settings.STATIC_URL + '/ide/img/status/failing.png')

@login_required
@require_POST
def transition_accept(request):
    user_settings = request.user.settings
    user_settings.accepted_terms = True
    user_settings.save()
    send_keen_event('cloudpebble', 'cloudpebble_ownership_transition_accepted', request=request)
    return json_response({})

@login_required
@require_POST
def transition_export(request):
    task = export_user_projects.delay(request.user.id)
    return json_response({"task_id": task.task_id})

@login_required
@require_POST
def transition_delete(request):
    send_keen_event('cloudpebble', 'cloudpebble_ownership_transition_declined', request=request)
    request.user.delete()
    return json_response({})

@login_required
@require_POST
def proxy_keen(request, project_id):
    project = get_object_or_404(Project, pk=project_id)

    acceptable_events = {
        'app_install_succeeded',
        'websocket_connection_failed',
        'app_install_failed',
        'app_log_view',
        'sdk_screenshot_success',
        'sdk_screenshot_failed'
    }

    event = request.POST['event']
    if event not in acceptable_events:
        return json_failure("nope.")

    data = {'data': json.loads(request.POST['data'])} if 'data' in request.POST else None

    send_keen_event(['cloudpebble', 'sdk'], event, project=project, request=request, data=data)
    return json_response({})

@login_required
@require_safe
def list_phones(request):
    user_key = request.user.social_auth.get(provider='pebble').extra_data['access_token']

    response = requests.get(
        '{0}/api/v1/me.json'.format(settings.SOCIAL_AUTH_PEBBLE_ROOT_URL),
        headers={'Authorization': 'Bearer {0}'.format(user_key)},
        params={'client_id': settings.SOCIAL_AUTH_PEBBLE_KEY})

    if response.status_code != 200:
        return json_failure(response.reason)
    else:
        devices = response.json()['devices']
        return json_response({'devices': devices})

@login_required
@require_POST
def ping_phone(request):
    user_id = request.user.social_auth.get(provider='pebble').uid
    device = request.POST['device']

    check_token = uuid.uuid4().hex

    print '{0}/api/v1/users/{1}/devices/{2}/push'.format(settings.SOCIAL_AUTH_PEBBLE_ROOT_URL, user_id, device)
    print {
            'admin_token': settings.PEBBLE_AUTH_ADMIN_TOKEN,
            'message': "Tap to enable developer mode and installs apps from CloudPebble",
            'custom': json.dumps({
                'action': 'sdkconnect',
                'token': check_token
            })
        }

    requests.post(
        '{0}/api/v1/users/{1}/devices/{2}/push'.format(settings.SOCIAL_AUTH_PEBBLE_ROOT_URL, user_id, device),
        params={
            'admin_token': settings.PEBBLE_AUTH_ADMIN_TOKEN,
            'message': "Tap to enable developer mode and installs apps from CloudPebble",
            'custom': json.dumps({
                'action': 'sdkconnect',
                'token': check_token
            })
        }
    )

    return json_response({'token': check_token})

@login_required
@require_safe
def check_phone(request, request_id):
    ip = redis_client.get('phone-ip-{0}'.format(request_id))
    return json_response({'ip': ip})

@require_POST
@csrf_exempt
def update_phone(request):
    data = json.loads(request.body)
    redis_client.set('phone-ip-{0}'.format(data['token']), data['ip'], ex=120)
    return json_response({})
