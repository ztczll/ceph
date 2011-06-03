from gevent import monkey; monkey.patch_all()
from orchestra import monkey; monkey.patch_all()

from cStringIO import StringIO

import bunch
import logging
import os
import sys
import yaml

from orchestra import connection, run, remote
import orchestra.cluster
# TODO cleanup
import teuthology.misc as teuthology

log = logging.getLogger(__name__)

if __name__ == '__main__':
    logging.basicConfig(
        # level=logging.INFO,
        level=logging.DEBUG,
        )

    with file('dbench.yaml') as f:
        config = yaml.safe_load(f)

    ROLES = config['roles']

    connections = [connection.connect(t) for t in config['targets']]
    remotes = [remote.Remote(name=t, ssh=c) for c,t in zip(connections, config['targets'])]
    cluster = orchestra.cluster.Cluster()
    for rem, roles in zip(remotes, ROLES):
        cluster.add(rem, roles)

    log.info('Checking for old test directory...')
    processes = cluster.run(
        args=[
            'test', '!', '-e', '/tmp/cephtest',
            ],
        wait=False,
        )
    try:
        run.wait(processes)
    except run.CommandFailedError as e:
        log.error('Host %s has stale cephtest directory, check your lock and reboot to clean up.', rem)
        sys.exit(1)

    log.info('Creating directories...')
    run.wait(
        cluster.run(
            args=[
                'install', '-d', '-m0755', '--',
                '/tmp/cephtest/binary',
                '/tmp/cephtest/log',
                '/tmp/cephtest/profiling-logger',
                '/tmp/cephtest/data',
                '/tmp/cephtest/class_tmp',
                ],
            wait=False,
            )
        )

    for filename in ['daemon-helper']:
        log.info('Shipping %r...', filename)
        src = os.path.join(os.path.dirname(__file__), filename)
        dst = os.path.join('/tmp/cephtest', filename)
        with file(src, 'rb') as f:
            for rem in cluster.remotes.iterkeys():
                teuthology.write_file(
                    remote=rem,
                    path=dst,
                    data=f,
                    )
                f.seek(0)
                rem.run(
                    args=[
                        'chmod',
                        'a=rx',
                        '--',
                        dst,
                        ],
                    )

    log.info('Untarring ceph binaries...')
    ceph_bindir_url = teuthology.get_ceph_binary_url()
    cluster.run(
        args=[
            'uname', '-m',
            run.Raw('|'),
            'sed', '-e', 's/^/ceph./; s/$/.tgz/',
            run.Raw('|'),
            'wget',
            '-nv',
            '-O-',
            '--base={url}'.format(url=ceph_bindir_url),
            # need to use --input-file to make wget respect --base
            '--input-file=-',
            run.Raw('|'),
            'tar', '-xzf', '-', '-C', '/tmp/cephtest/binary',
            ],
        )

    log.info('Writing configs...')
    ips = [host for (host, port) in (conn.get_transport().getpeername() for conn in connections)]
    conf = teuthology.skeleton_config(roles=ROLES, ips=ips)
    conf_fp = StringIO()
    conf.write(conf_fp)
    conf_fp.seek(0)
    writes = cluster.run(
        args=[
            'python',
            '-c',
            'import shutil, sys; shutil.copyfileobj(sys.stdin, file(sys.argv[1], "wb"))',
            '/tmp/cephtest/ceph.conf',
            ],
        stdin=run.PIPE,
        wait=False,
        )
    teuthology.feed_many_stdins_and_close(conf_fp, writes)
    run.wait(writes)

    log.info('Setting up mon.0...')
    cluster.only('mon.0').run(
        args=[
            '/tmp/cephtest/binary/usr/local/bin/cauthtool',
            '--create-keyring',
            '/tmp/cephtest/ceph.keyring',
            ],
        )
    cluster.only('mon.0').run(
        args=[
            '/tmp/cephtest/binary/usr/local/bin/cauthtool',
            '--gen-key',
            '--name=mon.',
            '/tmp/cephtest/ceph.keyring',
            ],
        )
    (mon0_remote,) = cluster.only('mon.0').remotes.keys()
    teuthology.create_simple_monmap(
        remote=mon0_remote,
        conf=conf,
        )

    log.info('Creating admin key on mon.0...')
    cluster.only('mon.0').run(
        args=[
            '/tmp/cephtest/binary/usr/local/bin/cauthtool',
            '--gen-key',
            '--name=client.admin',
            '--set-uid=0',
            '--cap', 'mon', 'allow *',
            '--cap', 'osd', 'allow *',
            '--cap', 'mds', 'allow',
            '/tmp/cephtest/ceph.keyring',
            ],
        )

    log.info('Copying mon.0 info to all monitors...')
    keyring = teuthology.get_file(
        remote=mon0_remote,
        path='/tmp/cephtest/ceph.keyring',
        )
    monmap = teuthology.get_file(
        remote=mon0_remote,
        path='/tmp/cephtest/monmap',
        )
    mons = cluster.only(teuthology.is_type('mon'))
    mons_no_0 = mons.exclude('mon.0')

    for rem in mons_no_0.remotes.iterkeys():
        # copy mon key and initial monmap
        log.info('Sending mon0 info to node {remote}'.format(remote=rem))
        teuthology.write_file(
            remote=rem,
            path='/tmp/cephtest/ceph.keyring',
            data=keyring,
            )
        teuthology.write_file(
            remote=rem,
            path='/tmp/cephtest/monmap',
            data=monmap,
            )

    log.info('Setting up mon nodes...')
    run.wait(
        mons.run(
            args=[
                '/tmp/cephtest/binary/usr/local/bin/osdmaptool',
                '--clobber',
                '--createsimple', '{num:d}'.format(
                    num=teuthology.num_instances_of_type(ROLES, 'osd'),
                    ),
                '/tmp/cephtest/osdmap',
                '--pg_bits', '2',
                '--pgp_bits', '4',
                ],
            wait=False,
            ),
        )

    for id_ in teuthology.all_roles_of_type(ROLES, 'mon'):
        (rem,) = cluster.only('mon.{id}'.format(id=id_)).remotes.keys()
        rem.run(
            args=[
                '/tmp/cephtest/binary/usr/local/bin/cmon',
                '--mkfs',
                '-i', id_,
                '-c', '/tmp/cephtest/ceph.conf',
                '--monmap=/tmp/cephtest/monmap',
                '--osdmap=/tmp/cephtest/osdmap',
                '--keyring=/tmp/cephtest/ceph.keyring',
                ],
            )

    run.wait(
        mons.run(
            args=[
                'rm',
                '--',
                '/tmp/cephtest/monmap',
                '/tmp/cephtest/osdmap',
                ],
            wait=False,
            ),
        )

    mon_daemons = {}
    log.info('Starting mon daemons...')
    for idx, roles_for_host in enumerate(ROLES):
        for id_ in teuthology.roles_of_type(roles_for_host, 'mon'):
            proc = run.run(
                client=connections[idx],
                args=[
                    '/tmp/cephtest/daemon-helper',
                    '/tmp/cephtest/binary/usr/local/bin/cmon',
                    '-f',
                    '-i', id_,
                    '-c', '/tmp/cephtest/ceph.conf',
                    ],
                logger=log.getChild('mon.{id}'.format(id=id_)),
                stdin=run.PIPE,
                wait=False,
                )
            mon_daemons[id_] = proc

    log.info('Setting up osd nodes...')
    for idx, roles_for_host in enumerate(ROLES):
        for id_ in teuthology.roles_of_type(roles_for_host, 'osd'):
            run.run(
                client=connections[idx],
                args=[
                    '/tmp/cephtest/binary/usr/local/bin/cauthtool',
                    '--create-keyring',
                    '--gen-key',
                    '--name=osd.{id}'.format(id=id_),
                    '/tmp/cephtest/data/osd.{id}.keyring'.format(id=id_),
                    ],
                )

    log.info('Setting up mds nodes...')
    for idx, roles_for_host in enumerate(ROLES):
        for id_ in teuthology.roles_of_type(roles_for_host, 'mds'):
            run.run(
                client=connections[idx],
                args=[
                    '/tmp/cephtest/binary/usr/local/bin/cauthtool',
                    '--create-keyring',
                    '--gen-key',
                    '--name=mds.{id}'.format(id=id_),
                    '/tmp/cephtest/data/mds.{id}.keyring'.format(id=id_),
                    ],
                )

    log.info('Setting up client nodes...')
    clients = cluster.only(teuthology.is_type('client'))
    for id_ in teuthology.all_roles_of_type(ROLES, 'client'):
        (rem,) = cluster.only('client.{id}'.format(id=id_)).remotes.keys()
        rem.run(
            args=[
                '/tmp/cephtest/binary/usr/local/bin/cauthtool',
                '--create-keyring',
                '--gen-key',
                # TODO this --name= is not really obeyed, all unknown "types" are munged to "client"
                '--name=client.{id}'.format(id=id_),
                '/tmp/cephtest/data/client.{id}.keyring'.format(id=id_),
                ],
            )

    log.info('Reading keys from all nodes...')
    keys = []
    for idx, roles_for_host in enumerate(ROLES):
        for type_ in ['osd','mds','client']:
            for id_ in teuthology.roles_of_type(roles_for_host, type_):
                data = teuthology.get_file(
                    remote=remotes[idx],
                    path='/tmp/cephtest/data/{type}.{id}.keyring'.format(
                        type=type_,
                        id=id_,
                        ),
                    )
                keys.append((type_, id_, data))

    log.info('Adding keys to mon.0...')
    for type_, id_, data in keys:
        teuthology.write_file(
            remote=mon0_remote,
            path='/tmp/cephtest/temp.keyring',
            data=data,
            )
        mon0_remote.run(
            args=[
                '/tmp/cephtest/binary/usr/local/bin/cauthtool',
                '/tmp/cephtest/temp.keyring',
                '--name={type}.{id}'.format(
                    type=type_,
                    id=id_,
                    ),
                ] + list(teuthology.generate_caps(type_)),
            )
        mon0_remote.run(
            args=[
                '/tmp/cephtest/binary/usr/local/bin/ceph',
                '-c', '/tmp/cephtest/ceph.conf',
                '-k', '/tmp/cephtest/ceph.keyring',
                '-i', '/tmp/cephtest/temp.keyring',
                'auth',
                'add',
                '{type}.{id}'.format(
                    type=type_,
                    id=id_,
                    ),
                ],
            )

    log.info('Setting max_mds...')
    # TODO where does this belong?
    mon0_remote.run(
        args=[
            '/tmp/cephtest/binary/usr/local/bin/ceph',
            '-c', '/tmp/cephtest/ceph.conf',
            '-k', '/tmp/cephtest/ceph.keyring',
            'mds',
            'set_max_mds',
            '{num_mds:d}'.format(
                num_mds=teuthology.num_instances_of_type(ROLES, 'mds'),
                ),
            ],
        )

    log.info('Running mkfs on osd nodes...')
    for id_ in teuthology.all_roles_of_type(ROLES, 'osd'):
        (rem,) = cluster.only('osd.{id}'.format(id=id_)).remotes.keys()
        rem.run(
            args=[
                'mkdir',
                os.path.join('/tmp/cephtest/data', 'osd.{id}.data'.format(id=id_)),
                ],
            )
        rem.run(
            args=[
                '/tmp/cephtest/binary/usr/local/bin/cosd',
                '--mkfs',
                '-i', id_,
                '-c', '/tmp/cephtest/ceph.conf'
                ],
            )

    osd_daemons = {}
    log.info('Starting osd daemons...')
    for idx, roles_for_host in enumerate(ROLES):
        for id_ in teuthology.roles_of_type(roles_for_host, 'osd'):
            proc = run.run(
                client=connections[idx],
                args=[
                    '/tmp/cephtest/daemon-helper',
                    '/tmp/cephtest/binary/usr/local/bin/cosd',
                    '-f',
                    '-i', id_,
                    '-c', '/tmp/cephtest/ceph.conf'
                    ],
                logger=log.getChild('osd.{id}'.format(id=id_)),
                stdin=run.PIPE,
                wait=False,
                )
            osd_daemons[id_] = proc

    mds_daemons = {}
    log.info('Starting mds daemons...')
    for idx, roles_for_host in enumerate(ROLES):
        for id_ in teuthology.roles_of_type(roles_for_host, 'mds'):
            proc = run.run(
                client=connections[idx],
                args=[
                    '/tmp/cephtest/daemon-helper',
                    '/tmp/cephtest/binary/usr/local/bin/cmds',
                    '-f',
                    '-i', id_,
                    '-c', '/tmp/cephtest/ceph.conf'
                    ],
                logger=log.getChild('mds.{id}'.format(id=id_)),
                stdin=run.PIPE,
                wait=False,
                )
            mds_daemons[id_] = proc


    log.info('Waiting until ceph is healthy...')
    teuthology.wait_until_healthy(
        remote=mon0_remote,
        )

    # TODO kclient mount/umount

    # TODO rbd

    ctx = bunch.Bunch(
        cluster=cluster,
        )
    from teuthology.run_tasks import run_tasks
    run_tasks(
        tasks=[
            {'cfuse': ['client.0']},
            {'autotest': {'client.0': ['dbench']}},
            {'interactive': None},
            ],
        ctx=ctx,
        )

    log.info('Shutting down mds daemons...')
    for id_, proc in mds_daemons.iteritems():
        proc.stdin.close()
    run.wait(mds_daemons.itervalues())

    log.info('Shutting down osd daemons...')
    for id_, proc in osd_daemons.iteritems():
        proc.stdin.close()
    run.wait(osd_daemons.itervalues())

    log.info('Shutting down mon daemons...')
    for id_, proc in mon_daemons.iteritems():
        proc.stdin.close()
    run.wait(mon_daemons.itervalues())
