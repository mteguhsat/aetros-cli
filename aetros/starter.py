from __future__ import print_function, division
from __future__ import absolute_import

import re
import time
import json
import os
import subprocess
import sys
import six

from aetros.logger import GeneralLogger
from aetros.utils import unpack_full_job_id, read_home_config, flatten_parameters, get_ssh_key_for_host
from aetros.const import JOB_STATUS
from .backend import JobBackend
from .Trainer import Trainer

class GitCommandException(Exception):
    cmd = None


def start(logger, full_id, fetch=True, env=None, volumes=None, gpu_devices=None):
    """
    Starts the training process with all logging of a job_id
    """

    owner, name, id = unpack_full_job_id(full_id)

    if isinstance(sys.stdout, GeneralLogger):
        # we don't want to have stuff written to stdout before in job's log
        sys.stdout.clear_buffer()

    job_backend = JobBackend(model_name=owner + '/' + name)

    if fetch:
        job_backend.fetch(id)

    job_backend.restart(id)
    job_backend.start(collect_system=False)
    job_backend.set_status('PREPARE')
    job_backend.monitoring_thread.handle_max_time = False

    start_command(logger, job_backend, env, volumes, gpu_devices=gpu_devices)


def start_command(logger, job_backend, env_overwrite=None, volumes=None, gpu_devices=None):
    work_tree = job_backend.git.work_tree
    home_config = read_home_config()

    env = {}
    if env_overwrite:
        env.update(env_overwrite)

    start_time = time.time()
    env['AETROS_MODEL_NAME'] = job_backend.model_name
    env['AETROS_JOB_ID'] = str(job_backend.job_id)
    env['DEBUG'] = os.getenv('DEBUG', '')
    env['AETROS_ATTY'] = '1'
    env['AETROS_GIT'] = job_backend.git.get_base_command()

    env['PATH'] = os.getenv('PATH', '')
    if 'PYTHONPATH' not in env:
        env['PYTHONPATH'] = os.getenv('PYTHONPATH', '')

    if os.getenv('AETROS_SSH_KEY_BASE64'):
        env['AETROS_SSH_KEY_BASE64'] = os.getenv('AETROS_SSH_KEY_BASE64')
    elif get_ssh_key_for_host(home_config['host']):
        # we need to read the key into env so the docker container can connect to AETROS
        env['AETROS_SSH_KEY_BASE64'] = open(get_ssh_key_for_host(home_config['host']), 'r').read()

    job_config = job_backend.job['config']

    if 'command' not in job_config:
        job_backend.fail('No "command" given. See Configuration section in the documentation.')

    job_commands = job_config['command']
    docker_image = job_config['image']

    if job_backend.is_simple_model():
        if docker_image:
            job_commands = ['python']
        else:
            job_commands = [sys.executable]
        job_commands += ['-m', 'aetros', 'start-simple', job_backend.model_name + '/' + job_backend.job_id]

    if job_commands is None:
        raise Exception('No command specified.')

    if not isinstance(job_commands, list) and not isinstance(job_commands, dict):
        job_commands = [job_commands]

    # replace {{batch_size}} parameters
    if isinstance(job_config['parameters'], dict):
        for key, value in six.iteritems(flatten_parameters(job_config['parameters'])):
            if isinstance(job_commands, list):
                for k, v in enumerate(job_commands):
                    if isinstance(job_commands[k], six.string_types):
                        job_commands[k] = job_commands[k].replace('{{' + key + '}}', json.dumps(value))

            elif isinstance(job_commands, dict):
                for k, v in six.iteritems(job_commands):
                    if isinstance(job_commands[k], six.string_types):
                        job_commands[k] = job_commands[k].replace('{{' + key + '}}', json.dumps(value))

    job_backend.set_system_info('commands', job_commands)
    logger.info("Switch working directory to " + work_tree)
    os.chdir(job_backend.git.work_tree)

    docker_image_built = False

    if job_config['dockerfile'] or job_config['install']:
        rebuild_image = job_config['rebuild_image'] if 'rebuild_image' in job_config else False
        docker_image = docker_build_image(logger, home_config, job_backend, rebuild_image)
        docker_image_built = True

    if docker_image:
        if not docker_image_built:
            docker_pull_image(logger, home_config, job_backend)

        docker_image_information(logger, home_config, job_backend)

        # make sure old container is removed
        subprocess.Popen([home_config['docker'], 'rm', job_backend.job_id], stderr=subprocess.PIPE).wait()

        command = docker_command_wrapper(logger, home_config, job_backend, volumes, gpu_devices, env)

        # since linux doesnt handle SIGINT when pid=1 process has no signal listener,
        # we need to make sure, we attached one to the pid=1 process
        trap = 'trapIt () { "$@"& pid="$!"; trap "kill -INT $pid" INT TERM; ' \
               'while kill -0 $pid > /dev/null 2>&1; do wait $pid; ec="$?"; done; exit $ec;};'

        command.append(docker_image)
        command += ['/bin/sh', '-c', trap + 'trapIt /bin/sh /job/aetros/command.sh']
    else:
        # non-docker

        # env['PYTHONPATH'] += ':' + os.getcwd()
        job_backend.collect_system_information()
        job_backend.collect_environment(env)

        if os.environ.get('LD_LIBRARY_PATH', None):
            # new shells unset LD_LIBRARY_PATH automatically, so we make sure it will be there again
            command = ['/bin/sh', '-c', 'export LD_LIBRARY_PATH=$LD_LIBRARY_PATH_ORI; /bin/sh "'+job_backend.git.work_tree+'/aetros/command.sh"']
        else:
            command = ['/bin/sh', job_backend.git.work_tree + '/job/aetros/command.sh']

    logger.debug("$ %s " % (' '.join([json.dumps(a) for a in command])))
    job_backend.set_system_info('image/name', str(docker_image))

    p = None
    exited = False
    last_return_code = None
    last_process = None
    all_done = False
    command_stats = None
    try:
        job_backend.set_status('STARTED')
        # logger.warning("$ %s " % (str(command),))

        # make sure maxTime limitation is correctly calculated
        job_backend.monitoring_thread.handle_max_time = True
        job_backend.monitoring_thread.handle_max_time_time = time.time()

        # Since JobBackend sends SIGINT to its current process group, it sends also to its parents when same pg.
        # We need to change the process group of the process, so this won't happen.
        # If we don't this, the master process (server command e.g.) receives the SIGINT as well.
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['preexec_fn'] = os.setsid

        # only use full env when no image used

        command_env = env
        if not docker_image:
            command_env = os.environ.copy()
            command_env.update(env)
            if os.environ.get('LD_LIBRARY_PATH', None):
                command_env['LD_LIBRARY_PATH_ORI'] = command_env['LD_LIBRARY_PATH']

        def write_command_sh(job_command):
            f = open(job_backend.git.work_tree + '/aetros/command.sh', 'w+')
            f.write(job_command)
            f.close()

        def exec_command(id, command, job_command):
            write_command_sh(job_command)
            print('$ ' + job_command.strip() + '\n')
            # args = [job_backend.job_id + '_' + str(id) if x == job_backend.job_id else x for x in command]
            args = command
            logger.debug('$ ' + ' '.join([json.dumps(a) for a in args]))
            p = subprocess.Popen(args=args, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE, env=command_env,
                **kwargs)

            # todo, start docker cpu,memory, assigned GPU monitoring when docker_image

            wait_stdout = sys.stdout.attach(p.stdout)
            wait_stderr = sys.stderr.attach(p.stderr)
            p.wait()
            wait_stdout()
            wait_stderr()
            return p

        done = 0
        total = len(job_commands)
        job_backend.set_system_info('command_stats', command_stats, True)
        if isinstance(job_commands, list):
            command_stats = [{'rc': None, 'started': None, 'ended': None} for x in job_commands]
            for k, job_command in enumerate(job_commands):
                job_backend.set_status('COMMAND #' + str(k))

                command_stats[k]['started'] = time.time() - start_time
                job_backend.set_system_info('command_stats', command_stats, True)

                command_env['AETROS_JOB_NAME'] = 'command_' + str(k)
                last_process = exec_command(k, command, job_command)
                last_return_code = last_process.poll()

                command_stats[k]['rc'] = last_return_code
                command_stats[k]['ended'] = time.time() - start_time
                job_backend.set_system_info('command_stats', command_stats, True)

                if last_return_code == 0:
                    done += 1
                else:
                    # one failed, so exit and don't execute next
                    break

        if isinstance(job_commands, dict):
            command_stats = {}
            for name, job_command in six.iteritems(job_commands):
                command_stats[name] = {'rc': None, 'started': None, 'ended': None}

            for name, job_command in six.iteritems(job_commands):
                job_backend.set_status(name)

                command_stats[name]['started'] = time.time() - start_time
                job_backend.set_system_info('command_stats', command_stats, True)

                # important to prefix it, otherwise name='master' would reset all stats in controller backend
                command_env['AETROS_JOB_NAME'] = 'command_' + name
                last_process = exec_command(name, command, job_command)
                last_return_code = last_process.poll()

                command_stats[name]['rc'] = last_return_code
                command_stats[name]['ended'] = time.time() - start_time
                job_backend.set_system_info('command_stats', command_stats, True)

                if last_return_code == 0:
                    done += 1
                else:
                    # one failed, so exit and don't execute next
                    break

        all_done = done == total
        exited = True

        if last_process:
            sys.exit(last_process.poll())
        else:
            sys.exit(1)

    except SystemExit:
        # since we started the command in a new process group, a SIGINT or CTRL+C on this process won't affect
        # our actual command process. So we need to take care that we stop everything.
        print("SystemExit, all-done=", str(all_done), 'last-process=', last_process is not None, last_process.poll() if last_process else None)

        # make sure the process dies
        if docker_image:
            # docker run does not proxy INT signals to the docker-engine,
            # so we need to do it on our own directly.
            print("stop docker container " + job_backend.job_id)
            subprocess.Popen([home_config['docker'], 'stop', job_backend.job_id], stderr=subprocess.PIPE,
                stdout=subprocess.PIPE).wait()
            print("stopped")
        elif not exited and last_process and last_process.poll() is None:
            # wait for last command
            last_process.kill()  # sends SIGINT
            last_process.wait()

        if exited:
            if all_done:
                job_backend.stop(progress=JOB_STATUS.PROGRESS_STATUS_DONE)
            elif last_return_code == 1:
                job_backend.stop(progress=JOB_STATUS.PROGRESS_STATUS_ABORTED)
            else:
                job_backend.stop(progress=JOB_STATUS.PROGRESS_STATUS_FAILED)
        else:
            # master received SIGINT before the all job commands exited.
            if not job_backend.in_early_stop:
                # in_early_stop indicates whether we want to have a planned stop (maxTime limitation for example),
                # which should mark the job as done, not as abort().
                # if this is not set, we the master received a SIGINT without early_stop, so mark as aborted.
                job_backend.abort()
            else:
                # let the on_shutdown listener handle the rest
                pass


def docker_pull_image(logger, home_config, job_backend):
    image = job_backend.job['config']['image']

    logger.info("Pull docker image: $ " + image)
    job_backend.set_status('IMAGE PULL')

    execute_command(args=[home_config['docker'], 'pull', image], bufsize=1, stderr=subprocess.PIPE,
        stdout=subprocess.PIPE)


def docker_image_information(logger, home_config, job_backend):
    image = job_backend.job['config']['image']

    inspections = execute_command_stdout([home_config['docker'], 'inspect', image])
    inspections = json.loads(inspections.decode('utf-8'))

    if inspections:
        inspection = inspections[0]
        with job_backend.git.batch_commit('Docker image'):
            job_backend.set_system_info('image/id', inspection['Id'])
            job_backend.set_system_info('image/docker_version', inspection['DockerVersion'])
            job_backend.set_system_info('image/created', inspection['Created'])
            job_backend.set_system_info('image/container', inspection['Container'])
            job_backend.set_system_info('image/architecture', inspection['Architecture'])
            job_backend.set_system_info('image/os', inspection['Os'])
            job_backend.set_system_info('image/size', inspection['Size'])
            job_backend.set_system_info('image/rootfs', inspection['RootFS'])


def docker_command_wrapper(logger, home_config, job_backend, volumes, gpu_devices, env):
    docker_command = [home_config['docker'], 'run', '-t', '--rm', '--name', job_backend.job_id]
    docker_command += home_config['docker_options']

    env['AETROS_GIT_WORK_DIR'] = '/job'
    docker_command += ['--mount', 'type=bind,source=' + job_backend.git.work_tree + ',destination=/job']

    if not os.path.exists(job_backend.git.work_tree + '/aetros/'):
        os.makedirs(job_backend.git.work_tree + '/aetros/')

    env['AETROS_STORAGE_DIR'] = '/aetros'
    docker_command += ['--mount',
        'type=bind,source=' + job_backend.git.git_path + ',destination=' + '/aetros/' + job_backend.model_name + '.git']

    home_config_path = os.path.expanduser('~/aetros.yml')
    if os.path.exists(home_config_path):
        env['AETROS_HOME_CONFIG_FILE'] = '/aetros/aetros.yml'
        docker_command += ['--mount', 'type=bind,source=' + home_config_path + ',destination=' + '/aetros/aetros.yml']

    docker_command += ['-w', '/job']

    # following makes no sense to pass to Docker
    env_blacklist = ['PATH', 'PYTHONPATH']

    # make sure the docker command receives all environment variables
    for k in six.iterkeys(env):
        if k in env_blacklist:
            continue
        docker_command += ['-e', k]

    docker_command += ['-e', 'AETROS_JOB_NAME']

    if volumes:
        for volume in volumes:
            docker_command += ['-v', volume]

    if 'resources' in job_backend.job:
        assigned_resources = job_backend.job['resources']

        cpus = 1
        if 'cpu' in assigned_resources and assigned_resources['cpu']:
            cpus = assigned_resources['cpu']
        docker_command += ['--cpus', str(cpus)]

        memory = 1
        if 'memory' in assigned_resources and assigned_resources['memory']:
            memory = assigned_resources['memory']

        docker_command += ['--memory', str(memory * 1024 * 1024 * 1024)]

    if gpu_devices and (sys.platform == "linux" or sys.platform == "linux2"):
        # only supported on linux
        docker_command += ['--runtime', 'nvidia']
        docker_command += ['-e', 'NVIDIA_VISIBLE_DEVICES=' + (','.join(gpu_devices))]
        # support nvidia-docker1 as well
        # docker_command += ['--device', '/dev/nvidia1']

    return docker_command


def docker_build_image(logger, home_config, job_backend, rebuild_image=False):
    job_config = job_backend.job['config']
    image = job_config['image']
    dockerfile = job_config['dockerfile']

    if isinstance(dockerfile, six.string_types) and os.path.exists(dockerfile):
        pass
    else:
        if isinstance(dockerfile, six.string_types):
            dockerfile_content = dockerfile
        elif isinstance(dockerfile, list) and len(dockerfile) > 0:
            dockerfile_content = "\n".join(dockerfile)
        else:
            if image is None:
                job_backend.fail("Image name missing, needed by `install` in aetros.yml")
            dockerfile_content = 'FROM ' + image + '\nRUN '

            if isinstance(job_config['install'], list):
                dockerfile_content += '\n RUN '.join(job_config['install'])
            else:
                dockerfile_content += job_config['install']

        dockerfile_content = '# CREATED BY AETROS because of "install" or "dockerfile" config in aetros.yml.\n' + dockerfile_content

        with open('Dockerfile.aetros', 'w') as f:
            f.write(dockerfile_content)

        dockerfile = 'Dockerfile.aetros'
        job_backend.commit_file('Dockerfile.aetros')

    job_backend.set_system_info('image/dockerfile', dockerfile)

    image = job_backend.model_name.lower()
    if 'category' in job_config:
        image += '_' + job_config['category'].lower()

    image = re.sub('[^A-Z_\-a-z0-9]+', '', image)
    docker_build = [home_config['docker'], 'build']

    if rebuild_image:
        docker_build += ['--no-cache']

    docker_build += ['-t', image, '-f', dockerfile, '.', ]

    logger.info("Prepare docker image: $ " + (' '.join(docker_build)))
    job_backend.set_status('IMAGE BUILD')
    p = execute_command(args=docker_build, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

    if p.returncode:
        job_backend.fail('Image build error')
        sys.exit(p.returncode)

    return image


def execute_command_stdout(command, input=None):
    p = subprocess.Popen(command, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    out, err = p.communicate(input)

    if p.returncode:
        sys.stderr.write(out)
        sys.stderr.write(err)
        raise Exception('Could not execute command: ' + str(command))

    return out


def execute_command(**kwargs):
    p = subprocess.Popen(**kwargs)
    wait_stdout = sys.stdout.attach(p.stdout)
    wait_stderr = sys.stderr.attach(p.stderr)

    p.wait()

    wait_stdout()
    wait_stderr()

    return p


def git_execute(logger, repo_path, args):
    args = ['git', '--git-dir', repo_path + '/.git', '--work-tree', repo_path] + args
    logger.info("$ %s" % (' '.join(args),))

    p = execute_command(args=args, bufsize=1, stderr=subprocess.PIPE, stdout=subprocess.PIPE)

    if p.returncode != 0:
        exception = GitCommandException("Git command returned not 0. " + (' '.join(args)))
        exception.cmd = (' '.join(args))
        raise exception


def start_keras(logger, job_backend):
    if 'KERAS_BACKEND' not in os.environ:
        os.environ['KERAS_BACKEND'] = 'tensorflow'

    from . import keras_model_utils

    # we need to import keras here, so we know which backend is used (and whether GPU is used)
    os.chdir(job_backend.git.work_tree)
    logger.debug("Start simple model")

    # we use the source from the job commit directly
    with job_backend.git.batch_commit('Git Version'):
        job_backend.set_system_info('git_remote_url', job_backend.git.get_remote_url('origin'))
        job_backend.set_system_info('git_version', job_backend.git.job_id)

    # all our shapes are Tensorflow schema. (height, width, channels)
    import keras.backend
    if hasattr(keras.backend, 'set_image_dim_ordering'):
        keras.backend.set_image_dim_ordering('tf')

    if hasattr(keras.backend, 'set_image_data_format'):
        keras.backend.set_image_data_format('channels_last')

    from .KerasCallback import KerasCallback
    trainer = Trainer(job_backend)
    keras_logger = KerasCallback(job_backend, job_backend.logger)

    job_backend.progress(0, job_backend.job['config']['epochs'])

    logger.info("Start training")
    keras_model_utils.job_start(job_backend, trainer, keras_logger)

    job_backend.done()
