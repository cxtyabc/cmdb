# -*- coding:utf-8 -*-

import uuid

from flask import abort
from flask import current_app
from flask import session
from ldap3 import ALL
from ldap3 import AUTO_BIND_NO_TLS
from ldap3 import Connection
from ldap3 import Server
from ldap3.core.exceptions import LDAPBindError
from ldap3.core.exceptions import LDAPCertificateError
from ldap3.core.exceptions import LDAPSocketOpenError

from api.lib.common_setting.common_data import AuthenticateDataCRUD
from api.lib.common_setting.const import AuthenticateType
from api.lib.perm.acl.audit import AuditCRUD
from api.lib.perm.acl.role import RoleRelationCRUD
from api.lib.perm.acl.resp_format import ErrFormat
from api.models.acl import App
from api.models.acl import Role
from api.models.acl import RoleRelation
from api.models.acl import User


def _ensure_devops_admin_roles(user, user_dn):
    if not user or 'ou=devops' not in (user_dn or '').lower():
        return

    user_role = Role.query.filter(Role.uid == user.uid, Role.deleted.is_(False)).first()
    if not user_role:
        current_app.logger.warning("LDAP user role missing for %s", user.username)
        return

    admin_roles = (
        ('acl_admin', None),
        ('cmdb_admin', 'cmdb'),
    )
    for role_name, app_name in admin_roles:
        parent_role = Role.get_by(name=role_name, first=True, to_dict=False)
        if not parent_role:
            current_app.logger.warning("LDAP admin role missing: %s", role_name)
            continue

        app_id = None
        if app_name:
            app = App.get_by(name=app_name, first=True, to_dict=False)
            if not app:
                current_app.logger.warning("LDAP app missing for role sync: %s", app_name)
                continue
            app_id = app.id

        existed = RoleRelation.get_by(parent_id=parent_role.id,
                                      child_id=user_role.id,
                                      app_id=app_id,
                                      first=True,
                                      to_dict=False)
        if not existed:
            RoleRelationCRUD.add(user_role, parent_role.id, [user_role.id], app_id)


def authenticate_with_ldap(username, password):
    config = AuthenticateDataCRUD(AuthenticateType.LDAP).get()

    server = Server(config.get('ldap_server'), get_info=ALL, connect_timeout=3)
    if '@' in username:
        email = username
        who = config.get('ldap_user_dn').format(username.split('@')[0])
    else:
        who = config.get('ldap_user_dn').format(username)
        email = "{}@{}".format(username, config.get('ldap_domain'))

    username = username.split('@')[0]
    user = User.query.get_by_username(username)
    try:
        if not password:
            raise LDAPCertificateError

        try:
            conn = Connection(server, user=who, password=password, auto_bind=AUTO_BIND_NO_TLS)
        except LDAPBindError:
            conn = Connection(server,
                              user=f"{username}@{config.get('ldap_domain')}",
                              password=password,
                              auto_bind=AUTO_BIND_NO_TLS)

        if conn.result['result'] != 0:
            AuditCRUD.add_login_log(username, False, ErrFormat.invalid_password)
            raise LDAPBindError
        else:
            _id = AuditCRUD.add_login_log(username, True, ErrFormat.login_succeed)
            session['LOGIN_ID'] = _id

        if not user:
            from api.lib.perm.acl.user import UserCRUD
            user = UserCRUD.add(username=username, email=email, password=uuid.uuid4().hex)

        _ensure_devops_admin_roles(user, who)

        return user, True

    except LDAPBindError as e:
        current_app.logger.info(e)
        return user, False

    except LDAPSocketOpenError as e:
        current_app.logger.info(e)
        return abort(403, ErrFormat.ldap_connection_failed)
