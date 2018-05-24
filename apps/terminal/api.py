# -*- coding: utf-8 -*-
#
from collections import OrderedDict
import logging
import os
import uuid

from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.core.files.storage import default_storage
from django.http.response import HttpResponseRedirectBase
from django.http import HttpResponseNotFound
from django.conf import settings

import jms_storage

from rest_framework import viewsets, serializers
from rest_framework.views import APIView, Response
from rest_framework.permissions import AllowAny
from rest_framework_bulk import BulkModelViewSet

from common.utils import get_object_or_none
from .models import Terminal, Status, Session, Task
from .serializers import TerminalSerializer, StatusSerializer, \
    SessionSerializer, TaskSerializer, ReplaySerializer
from .hands import IsSuperUserOrAppUser, IsAppUser, \
    IsSuperUserOrAppUserOrUserReadonly
from .backends import get_command_storage, get_multi_command_storage, \
    SessionCommandSerializer

logger = logging.getLogger(__file__)


class TerminalViewSet(viewsets.ModelViewSet):
    queryset = Terminal.objects.filter(is_deleted=False)
    serializer_class = TerminalSerializer
    permission_classes = (IsSuperUserOrAppUserOrUserReadonly,)

    def create(self, request, *args, **kwargs):
        name = request.data.get('name')
        remote_ip = request.META.get('REMOTE_ADDR')
        x_real_ip = request.META.get('X-Real-IP')
        remote_addr = x_real_ip or remote_ip

        terminal = get_object_or_none(Terminal, name=name, is_deleted=False)
        if terminal:
            msg = 'Terminal name %s already used' % name
            return Response({'msg': msg}, status=409)

        serializer = self.serializer_class(data={
            'name': name, 'remote_addr': remote_addr
        })

        if serializer.is_valid():
            terminal = serializer.save()

            # App should use id, token get access key, if accepted
            token = uuid.uuid4().hex
            cache.set(token, str(terminal.id), 3600)
            data = {"id": str(terminal.id), "token": token, "msg": "Need accept"}
            return Response(data, status=201)
        else:
            data = serializer.errors
            logger.error("Register terminal error: {}".format(data))
            return Response(data, status=400)

    def get_permissions(self):
        if self.action == "create":
            self.permission_classes = (AllowAny,)
        return super().get_permissions()


class TerminalTokenApi(APIView):
    permission_classes = (AllowAny,)
    queryset = Terminal.objects.filter(is_deleted=False)

    def get(self, request, *args, **kwargs):
        try:
            terminal = self.queryset.get(id=kwargs.get('terminal'))
        except Terminal.DoesNotExist:
            terminal = None

        token = request.query_params.get("token")

        if terminal is None:
            return Response('May be reject by administrator', status=401)

        if token is None or cache.get(token, "") != str(terminal.id):
            return Response('Token is not valid', status=401)

        if not terminal.is_accepted:
            return Response("Terminal was not accepted yet", status=400)

        if not terminal.user or not terminal.user.access_key.all():
            return Response("No access key generate", status=401)

        access_key = terminal.user.access_key.first()
        data = OrderedDict()
        data['access_key'] = {'id': access_key.id, 'secret': access_key.secret}
        return Response(data, status=200)


class StatusViewSet(viewsets.ModelViewSet):
    queryset = Status.objects.all()
    serializer_class = StatusSerializer
    permission_classes = (IsSuperUserOrAppUser,)
    session_serializer_class = SessionSerializer
    task_serializer_class = TaskSerializer

    def create(self, request, *args, **kwargs):
        from_gua = self.request.query_params.get("from_guacamole", None)
        if not from_gua:
            self.handle_sessions()
        super().create(request, *args, **kwargs)
        tasks = self.request.user.terminal.task_set.filter(is_finished=False)
        serializer = self.task_serializer_class(tasks, many=True)
        return Response(serializer.data, status=201)

    def handle_sessions(self):
        sessions_active = []
        for session_data in self.request.data.get("sessions", []):
            self.create_or_update_session(session_data)
            if not session_data["is_finished"]:
                sessions_active.append(session_data["id"])

        sessions_in_db_active = Session.objects.filter(
            is_finished=False,
            terminal=self.request.user.terminal.id
        )

        for session in sessions_in_db_active:
            if str(session.id) not in sessions_active:
                session.is_finished = True
                session.date_end = timezone.now()
                session.save()

    def create_or_update_session(self, session_data):
        session_data["terminal"] = self.request.user.terminal.id
        _id = session_data["id"]
        session = get_object_or_none(Session, id=_id)
        if session:
            serializer = SessionSerializer(
                data=session_data, instance=session
            )
        else:
            serializer = SessionSerializer(data=session_data)

        if serializer.is_valid():
            session = serializer.save()
            return session
        else:
            msg = "session data is not valid {}: {}".format(
                serializer.errors, str(serializer.data)
            )
            logger.error(msg)
            return None

    def get_queryset(self):
        terminal_id = self.kwargs.get("terminal", None)
        if terminal_id:
            terminal = get_object_or_404(Terminal, id=terminal_id)
            self.queryset = terminal.status_set.all()
        return self.queryset

    def perform_create(self, serializer):
        serializer.validated_data["terminal"] = self.request.user.terminal
        return super().perform_create(serializer)

    def get_permissions(self):
        if self.action == "create":
            self.permission_classes = (IsAppUser,)
        return super().get_permissions()


class SessionViewSet(viewsets.ModelViewSet):
    queryset = Session.objects.all()
    serializer_class = SessionSerializer
    permission_classes = (IsSuperUserOrAppUser,)

    def get_queryset(self):
        terminal_id = self.kwargs.get("terminal", None)
        if terminal_id:
            terminal = get_object_or_404(Terminal, id=terminal_id)
            self.queryset = terminal.session_set.all()
        return self.queryset

    def perform_create(self, serializer):
        if self.request.user.terminal:
            serializer.validated_data["terminal"] = self.request.user.terminal
        return super().perform_create(serializer)


class TaskViewSet(BulkModelViewSet):
    queryset = Task.objects.all()
    serializer_class = TaskSerializer
    permission_classes = (IsSuperUserOrAppUser,)


class KillSessionAPI(APIView):
    permission_classes = (IsSuperUserOrAppUser,)
    model = Task

    def post(self, request, *args, **kwargs):
        validated_session = []
        for session_id in request.data:
            session = get_object_or_none(Session, id=session_id)
            if session and not session.is_finished:
                validated_session.append(session_id)
                self.model.objects.create(
                    name="kill_session", args=session.id,
                    terminal=session.terminal,
                )
        return Response({"ok": validated_session})


class CommandViewSet(viewsets.ViewSet):
    """接受app发送来的command log, 格式如下
    {
        "user": "admin",
        "asset": "localhost",
        "system_user": "web",
        "session": "xxxxxx",
        "input": "whoami",
        "output": "d2hvbWFp",  # base64.b64encode(s)
        "timestamp": 1485238673.0
    }

    """
    command_store = get_command_storage()
    multi_command_storage = get_multi_command_storage()
    serializer_class = SessionCommandSerializer
    permission_classes = (IsSuperUserOrAppUser,)

    def get_queryset(self):
        self.command_store.filter(**dict(self.request.query_params))

    def create(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data, many=True)
        if serializer.is_valid():
            ok = self.command_store.bulk_save(serializer.validated_data)
            if ok:
                return Response("ok", status=201)
            else:
                return Response("Save error", status=500)
        else:
            msg = "Not valid: {}".format(serializer.errors)
            logger.error(msg)
            return Response({"msg": msg}, status=401)

    def list(self, request, *args, **kwargs):
        queryset = self.multi_command_storage.filter()
        serializer = self.serializer_class(queryset, many=True)
        return Response(serializer.data)


class SessionReplayViewSet(viewsets.ViewSet):
    serializer_class = ReplaySerializer
    permission_classes = (IsSuperUserOrAppUser,)
    session = None

    def gen_session_path(self):
        date = self.session.date_start.strftime('%Y-%m-%d')
        return os.path.join(date, str(self.session.id) + '.gz')

    def create(self, request, *args, **kwargs):
        session_id = kwargs.get('pk')
        self.session = get_object_or_404(Session, id=session_id)
        serializer = self.serializer_class(data=request.data)

        if serializer.is_valid():
            file = serializer.validated_data['file']
            file_path = self.gen_session_path()
            try:
                default_storage.save(file_path, file)
                return Response({'url': default_storage.url(file_path)},
                                status=201)
            except IOError:
                return Response("Save error", status=500)
        else:
            logger.error(
                'Update load data invalid: {}'.format(serializer.errors))
            return Response({'msg': serializer.errors}, status=401)

    def retrieve(self, request, *args, **kwargs):
        session_id = kwargs.get('pk')
        self.session = get_object_or_404(Session, id=session_id)
        path = self.gen_session_path()

        if default_storage.exists(path):
            url = default_storage.url(path)
            return redirect(url)
        else:
            configs = settings.TERMINAL_REPLAY_STORAGE
            configs = [cfg for cfg in configs if cfg['TYPE'] != 'server']
            if not configs:
                return HttpResponseNotFound()

            date = self.session.date_start.strftime('%Y-%m-%d')
            file_path = os.path.join(date, str(self.session.id) + '.replay.gz')
            target_path = default_storage.base_location + '/' + path
            storage = jms_storage.get_multi_object_storage(configs)
            ok, err = storage.download(file_path, target_path)
            if ok:
                return redirect(default_storage.url(path))
            else:
                logger.error("Failed download replay file: {}".format(err))
        return HttpResponseNotFound()


class SessionReplayV2ViewSet(SessionReplayViewSet):
    serializer_class = ReplaySerializer
    permission_classes = (IsSuperUserOrAppUser,)
    session = None

    def retrieve(self, request, *args, **kwargs):
        response = super().retrieve(request, *args, **kwargs)
        data = {
            'type': 'guacamole' if self.session.protocol == 'rdp' else 'json',
            'src': '',
        }
        if isinstance(response, HttpResponseRedirectBase):
            data['src'] = response.url
            return Response(data)
        return HttpResponseNotFound()


class TerminalConfig(APIView):
    permission_classes = (IsAppUser,)

    def get(self, request):
        user = request.user
        terminal = user.terminal
        configs = terminal.config
        return Response(configs, status=200)
