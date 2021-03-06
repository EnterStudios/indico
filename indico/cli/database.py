# This file is part of Indico.
# Copyright (C) 2002 - 2017 European Organization for Nuclear Research (CERN).
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or (at your option) any later version.
#
# Indico is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Indico; if not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import os
import sys
from functools import partial

import alembic.command
import click
from alembic.script import ScriptDirectory
from flask import current_app
from flask.cli import with_appcontext
from flask_migrate import stamp
from flask_migrate.cli import db as flask_migrate_cli
from flask_pluginengine import current_plugin

from indico.cli.core import cli_group
from indico.core.db import db
from indico.core.db.sqlalchemy.migration import migrate
from indico.core.db.sqlalchemy.protection import ProtectionMode
from indico.core.db.sqlalchemy.util.management import get_all_tables, create_all_tables
from indico.core.db.sqlalchemy.util.queries import has_extension
from indico.core.plugins import plugin_engine
from indico.modules.categories import Category
from indico.modules.users import User
from indico.util.console import cformat


@cli_group()
@click.option('--plugin', metavar='PLUGIN', help='Execute the command for the given plugin')
@click.option('--all-plugins', is_flag=True, help='Execute the command for all plugins')
@click.pass_context
@with_appcontext
def cli(ctx, plugin=None, all_plugins=False):
    if plugin and all_plugins:
        raise click.BadParameter('cannot combine --plugin and --all-plugins')
    if all_plugins and ctx.invoked_subcommand in ('migrate', 'revision', 'downgrade', 'stamp', 'edit'):
        raise click.UsageError('this command requires an explicit plugin')
    if (all_plugins or plugin) and ctx.invoked_subcommand == 'prepare':
        raise click.UsageError('this command is not available for plugins (use `upgrade` instead)')
    if plugin and not plugin_engine.get_plugin(plugin):
        raise click.BadParameter('plugin does not exist or is not loaded', param_hint='plugin')
    migrate.init_app(current_app, db, os.path.join(current_app.root_path, 'migrations'))


def _require_extensions(*names):
    missing = sorted(name for name in names if not has_extension(db.engine, name))
    if not missing:
        return True
    print cformat('%{red}Required Postgres extensions missing: {}').format(', '.join(missing))
    print cformat('%{yellow}Create them using these SQL commands (as a Postgres superuser):')
    for name in missing:
        print cformat('%{white!}  CREATE EXTENSION {};').format(name)
    return False


def _require_pg_version(version):
    # convert version string such as '9.4.10' to `90410` which is the
    # format used by server_version_num
    req_version = sum(segment * 10**(4 - 2*i) for i, segment in enumerate(map(int, version.split('.'))))
    cur_version = db.engine.execute("SELECT current_setting('server_version_num')::int").scalar()
    if cur_version >= req_version:
        return True
    print cformat('%{red}Postgres version too old; you need at least {} (or newer)').format(version)
    return False


@cli.command()
@click.option('--empty', is_flag=True, help='Do not create the root category or system user. Use this only if you '
                                            'intend to import data from ZODB.')
def prepare(empty):
    """Initializes an empty database (creates tables, sets alembic rev to HEAD)"""
    tables = get_all_tables(db)
    if 'alembic_version' not in tables['public']:
        print cformat('%{green}Setting the alembic version to HEAD')
        stamp(revision='heads')
        PluginScriptDirectory.dir = os.path.join(current_app.root_path, 'core', 'plugins', 'alembic')
        alembic.command.ScriptDirectory = PluginScriptDirectory
        plugin_msg = cformat("%{cyan}Setting the alembic version of the %{cyan!}{}%{reset}%{cyan} "
                             "plugin to HEAD%{reset}")
        for plugin in plugin_engine.get_active_plugins().itervalues():
            if not os.path.exists(plugin.alembic_versions_path):
                continue
            print plugin_msg.format(plugin.name)
            with plugin.plugin_context():
                stamp(revision='heads')
        # Retrieve the table list again, just in case we created unexpected hables
        tables = get_all_tables(db)

    tables['public'] = [t for t in tables['public'] if not t.startswith('alembic_version')]
    if any(tables.viewvalues()):
        print cformat('%{red}Your database is not empty!')
        print cformat('%{yellow}If you just added a new table/model, create an alembic revision instead!')
        print
        print 'Tables in your database:'
        for schema, schema_tables in sorted(tables.items()):
            for t in schema_tables:
                print cformat('  * %{cyan}{}%{reset}.%{cyan!}{}%{reset}').format(schema, t)
        return
    if not _require_pg_version('9.4'):
        return
    if not _require_extensions('unaccent', 'pg_trgm'):
        return
    create_all_tables(db, verbose=True, add_initial_data=(not empty))


def _safe_downgrade(*args, **kwargs):
    func = kwargs.pop('_func')
    print cformat('%{yellow!}*** DANGER')
    print cformat('%{yellow!}***%{reset} '
                  '%{red!}This operation may %{yellow!}PERMANENTLY ERASE %{red!}some data!%{reset}')
    if current_app.debug:
        skip_confirm = os.environ.get('INDICO_ALWAYS_DOWNGRADE', '').lower() in ('1', 'yes')
        print cformat('%{yellow!}***%{reset} '
                      "%{green!}Debug mode is active, so you probably won't destroy valuable data")
    else:
        skip_confirm = False
        print cformat('%{yellow!}***%{reset} '
                      "%{red!}Debug mode is NOT ACTIVE, so make sure you are on the right machine!")
    if not skip_confirm and raw_input(cformat('%{yellow!}***%{reset} '
                                              'To confirm this, enter %{yellow!}YES%{reset}: ')) != 'YES':
        print cformat('%{green}Aborted%{reset}')
        sys.exit(1)
    else:
        return func(*args, **kwargs)


class PluginScriptDirectory(ScriptDirectory):
    """Like `ScriptDirectory` but lets you override the paths from outside.

    This is a pretty ugly hack but alembic doesn't give us a nice way to do it...
    """

    dir = None
    versions = None

    def __init__(self, *args, **kwargs):
        super(PluginScriptDirectory, self).__init__(*args, **kwargs)
        self.dir = PluginScriptDirectory.dir
        # use __dict__ since it's a memoized property
        self.__dict__['_version_locations'] = [current_plugin.alembic_versions_path]

    @classmethod
    def from_config(cls, config):
        instance = super(PluginScriptDirectory, cls).from_config(config)
        instance.dir = PluginScriptDirectory.dir
        instance.__dict__['_version_locations'] = [current_plugin.alembic_versions_path]
        return instance


def _call_with_plugins(*args, **kwargs):
    func = kwargs.pop('_func')
    ctx = click.get_current_context()
    all_plugins = ctx.parent.params['all_plugins']
    plugin = ctx.parent.params['plugin']
    if plugin:
        plugins = {plugin_engine.get_plugin(plugin)}
    elif all_plugins:
        plugins = set(plugin_engine.get_active_plugins().viewvalues())
    else:
        plugins = None

    if plugins is None:
        func(*args, **kwargs)
    else:
        PluginScriptDirectory.dir = os.path.join(current_app.root_path, 'core', 'plugins', 'alembic')
        alembic.command.ScriptDirectory = PluginScriptDirectory
        for plugin in plugins:
            if not os.path.exists(plugin.alembic_versions_path):
                print cformat("%{cyan}skipping plugin '{}' (no migrations folder)").format(plugin.name)
                continue
            print cformat("%{cyan!}executing command for plugin '{}'").format(plugin.name)
            with plugin.plugin_context():
                func(*args, **kwargs)


def _setup_cli():
    for command in flask_migrate_cli.commands.itervalues():
        if command.name == 'init':
            continue
        command.callback = partial(with_appcontext(_call_with_plugins), _func=command.callback)
        if command.name == 'downgrade':
            command.callback = partial(with_appcontext(_safe_downgrade), _func=command.callback)
        cli.add_command(command)

_setup_cli()
del _setup_cli
