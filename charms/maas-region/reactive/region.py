# Copyright 2016-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

import fcntl
import os
from contextlib import contextmanager
from subprocess import (
    check_call,
    check_output,
)

from charmhelpers.core import (
    hookenv,
    host,
)
from charms.reactive import (
    endpoint_from_flag,
    hook,
    when,
    when_any,
    when_not,
    when_not_all,
    when_file_changed,
    set_state,
    set_flag,
    clear_flag,
)


@contextmanager
def lock_snap_context():
    """
    When both maas-region and maas-rack charms are placed on the same
    machine they need to be sure not to step over each other when
    running the commands in the snap.
    """
    fd = os.open('/tmp/maas-charm-lock', os.O_RDWR | os.O_CREAT, 600)
    fcntl.lockf(fd, fcntl.LOCK_EX)
    try:
        yield fd
    finally:
        os.close(fd)


def get_maas_secret():
    """Return the MAAS secret value."""
    secret = None
    with open('/var/snap/maas/current/var/lib/maas/secret', 'r') as fp:
        secret = fp.read().strip()
    if not secret:
        return None
    return secret


def get_snap_config_value(*args):
    """
    Return the current mode of the snap.
    """
    output = check_output([
        'maas', 'config',
        '--show',
        '--show-database-password',
        '--show-secret',
        '--parsable'])
    output = output.decode('utf-8')
    lines = output.splitlines()
    res = []
    for key in args:
        found = False
        for line in lines:
            line = line.strip()
            kvargs = line.split('=', 1)
            if len(kvargs) > 1 and kvargs[0] == key:
                res.append(kvargs[1])
                found = True
                break
        if not found:
            if key == 'secret':
                res.append(get_maas_secret())
            else:
                res.append(None)
    if len(res) == 1:
        return res[0]
    return res


def get_snap_mode(mode):
    """
    Return the mode the snap should change to.
    """
    current_mode = get_snap_config_value('mode')
    if mode == 'none':
        if current_mode == 'none':
            return 'none'
        if current_mode == 'rack':
            return 'rack'
        if current_mode == 'region':
            return 'none'
        if current_mode == 'region+rack':
            return 'rack'
        raise ValueError('Unknown operating mode: %s', current_mode)
    if mode == 'region':
        if current_mode == 'none':
            return 'region'
        if current_mode == 'rack':
            return 'region+rack'
        if current_mode == 'region':
            return 'region'
        if current_mode == 'region+rack':
            return 'region+rack'
        raise ValueError('Unknown operating mode: %s', current_mode)
    raise ValueError('Unknown operating mode: %s', current_mode)


def get_maas_url():
    maas_url = hookenv.config('maas-url')
    if maas_url:
        return maas_url
    return 'http://localhost:5240/MAAS'


def get_snap_args(mode, pgsql):
    set_mode = get_snap_mode(mode)
    args = ['--mode', set_mode]
    maas_url_set = False
    if set_mode != 'none' and set_mode != 'rack':
        conn_str = pgsql.master
        args += [
            '--database-host', conn_str['host'],
            '--database-name', conn_str['dbname'],
            '--database-user', conn_str['user'],
            '--database-pass', conn_str['password'],
            '--maas-url', get_maas_url(),
        ]
        maas_url_set = True
    if set_mode == 'region+rack' or set_mode == 'rack':
        secret, maas_url = get_snap_config_value('secret', 'maas_url')
        if not maas_url_set:
            args.append('--maas-url')
            args.append(maas_url)
        args.append('--secret')
        args.append(get_snap_config_value('secret'))
    return args


def is_maas_url_local(maas_url):
    if maas_url == 'http://localhost:5240/MAAS':
        return True
    return False


@when('snap.installed.maas')
@when_not('maas.snap.init', 'db.connected')
def missing_postgresql():
    hookenv.status_set('blocked', 'Waiting on relation to PostgreSQL')


@when('maas.snap.init', 'config.changed.maas-url')
def write_maas_url():
    hookenv.status_set('maintenance', 'Re-configuring controller')
    with lock_snap_context():
        check_call(
            ['maas', 'config'] +
            get_snap_args(
                'region', endpoint_from_flag('db.database.available')))
    hookenv.status_set('active')


@when('maas.snap.init')
@when_not('db.connected')
def disable_snap():
    hookenv.status_set('maintenance', 'Turning off controller')
    with lock_snap_context():
        check_call(['maas', 'config'] + get_snap_args('none', None))
    clear_flag('maas.snap.init')


@when('maas.snap.init', 'db.master.changed')
def write_db_config(pgsql):
    hookenv.status_set('maintenance', 'Configuring connection to database')
    with lock_snap_context():
        check_call(
            ['maas', 'config'] + get_snap_args('region', pgsql))
    clear_flag('db.master.changed')
    hookenv.status_set('active', 'Running')


@when('snap.installed.maas', 'db.master.available')
@when_not('maas.snap.init')
def init_db(pgsql):
    hookenv.status_set('maintenance', 'Initializing connection to database')
    with lock_snap_context():
        check_call(
            ['maas', 'init', '--force', '--skip-admin'] +
            get_snap_args('region', pgsql))
    set_flag('maas.snap.init')
    clear_flag('db.master.changed')
    hookenv.status_set('active', 'Running')


@when('maas.snap.init', 'endpoint.rpc.joined')
def rpc_requested(rpc):
    maas_url = get_maas_url()
    if is_maas_url_local(maas_url):
        maas_url = 'http://%s:5240/MAAS' % hookenv.unit_private_ip()
    secret = get_snap_config_value('secret')
    rpc.set_connection_info(maas_url, secret)


@when('maas.snap.init', 'endpoint.http.joined')
def http_connected(http):
    http.configure(5240)
