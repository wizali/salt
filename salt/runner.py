# -*- coding: utf-8 -*-
'''
Execute salt convenience routines
'''

# Import python libs
from __future__ import absolute_import, print_function
import os
import logging

# Import salt libs
import salt.exceptions
import salt.loader
import salt.minion
import salt.utils
import salt.utils.args
import salt.utils.event
from salt.client import mixins
from salt.output import display_output
from salt.utils.lazy import verify_fun

log = logging.getLogger(__name__)


class RunnerClient(mixins.SyncClientMixin, mixins.AsyncClientMixin, object):
    '''
    The interface used by the :command:`salt-run` CLI tool on the Salt Master

    It executes :ref:`runner modules <all-salt.runners>` which run on the Salt
    Master.

    Importing and using ``RunnerClient`` must be done on the same machine as
    the Salt Master and it must be done using the same user that the Salt
    Master is running as.

    Salt's :conf_master:`external_auth` can be used to authenticate calls. The
    eauth user must be authorized to execute runner modules: (``@runner``).
    Only the :py:meth:`master_call` below supports eauth.
    '''
    client = 'runner'
    tag_prefix = 'run'

    def __init__(self, opts):
        self.opts = opts

    @property
    def functions(self):
        if not hasattr(self, '_functions'):
            if not hasattr(self, 'utils'):
                self.utils = salt.loader.utils(self.opts)
            # Must be self.functions for mixin to work correctly :-/
            try:
                self._functions = salt.loader.runner(self.opts, utils=self.utils)
            except AttributeError:
                # Just in case self.utils is still not present (perhaps due to
                # problems with the loader), load the runner funcs without them
                self._functions = salt.loader.runner(self.opts)

        return self._functions

    def _reformat_low(self, low):
        '''
        Format the low data for RunnerClient()'s master_call() function

        The master_call function here has a different function signature than
        on WheelClient. So extract all the eauth keys and the fun key and
        assume everything else is a kwarg to pass along to the runner function
        to be called.
        '''
        auth_creds = dict([(i, low.pop(i)) for i in [
                'username', 'password', 'eauth', 'token', 'client', 'user', 'key'
            ] if i in low])
        fun = low.pop('fun')
        reformatted_low = {'fun': fun}
        reformatted_low.update(auth_creds)
        # Support old style calls where arguments could be specified in 'low' top level
        if not low.get('arg') and not low.get('kwarg'):  # not specified or empty
            verify_fun(self.functions, fun)
            merged_args_kwargs = salt.utils.args.condition_input([], low)
            parsed_input = salt.utils.args.parse_input(merged_args_kwargs)
            args, kwargs = salt.minion.load_args_and_kwargs(
                self.functions[fun],
                parsed_input,
                self.opts,
                ignore_invalid=True
            )
            low['arg'] = args
            low['kwarg'] = kwargs
        if 'kwarg' not in low:
            low['kwarg'] = {}
        if 'arg' not in low:
            low['arg'] = []
        reformatted_low['kwarg'] = low
        return reformatted_low

    def cmd_async(self, low):
        '''
        Execute a runner function asynchronously; eauth is respected

        This function requires that :conf_master:`external_auth` is configured
        and the user is authorized to execute runner functions: (``@runner``).

        .. code-block:: python

            runner.eauth_async({
                'fun': 'jobs.list_jobs',
                'username': 'saltdev',
                'password': 'saltdev',
                'eauth': 'pam',
            })
        '''
        reformatted_low = self._reformat_low(low)

        return mixins.AsyncClientMixin.cmd_async(self, reformatted_low)

    def cmd_sync(self, low, timeout=None):
        '''
        Execute a runner function synchronously; eauth is respected

        This function requires that :conf_master:`external_auth` is configured
        and the user is authorized to execute runner functions: (``@runner``).

        .. code-block:: python

            runner.eauth_sync({
                'fun': 'jobs.list_jobs',
                'username': 'saltdev',
                'password': 'saltdev',
                'eauth': 'pam',
            })
        '''
        reformatted_low = self._reformat_low(low)
        return mixins.SyncClientMixin.cmd_sync(self, reformatted_low, timeout)

    def cmd(self, fun, arg=None, pub_data=None, kwarg=None, print_event=True, full_return=False):
        '''
        Execute a function
        '''
        return super(RunnerClient, self).cmd(fun,
                                             arg,
                                             pub_data,
                                             kwarg,
                                             print_event,
                                             full_return)


class Runner(RunnerClient):
    '''
    Execute the salt runner interface
    '''
    def __init__(self, opts):
        super(Runner, self).__init__(opts)
        self.returners = salt.loader.returners(opts, self.functions)
        self.outputters = salt.loader.outputters(opts)

    def print_docs(self):
        '''
        Print out the documentation!
        '''
        arg = self.opts.get('fun', None)
        docs = super(Runner, self).get_docs(arg)
        for fun in sorted(docs):
            display_output('{0}:'.format(fun), 'text', self.opts)
            print(docs[fun])

    # TODO: move to mixin whenever we want a salt-wheel cli
    def run(self):
        '''
        Execute the runner sequence
        '''
        import salt.minion
        ret = {}
        if self.opts.get('doc', False):
            self.print_docs()
        else:
            low = {'fun': self.opts['fun']}
            try:
                verify_fun(self.functions, low['fun'])
                args, kwargs = salt.minion.load_args_and_kwargs(
                    self.functions[low['fun']],
                    salt.utils.args.parse_input(self.opts['arg']),
                    self.opts,
                )
                low['arg'] = args
                low['kwarg'] = kwargs

                if self.opts.get('eauth'):
                    if 'token' in self.opts:
                        try:
                            with salt.utils.fopen(os.path.join(self.opts['cachedir'], '.root_key'), 'r') as fp_:
                                low['key'] = fp_.readline()
                        except IOError:
                            low['token'] = self.opts['token']

                    # If using eauth and a token hasn't already been loaded into
                    # low, prompt the user to enter auth credentials
                    if 'token' not in low and 'key' not in low and self.opts['eauth']:
                        # This is expensive. Don't do it unless we need to.
                        import salt.auth
                        resolver = salt.auth.Resolver(self.opts)
                        res = resolver.cli(self.opts['eauth'])
                        if self.opts['mktoken'] and res:
                            tok = resolver.token_cli(
                                    self.opts['eauth'],
                                    res
                                    )
                            if tok:
                                low['token'] = tok.get('token', '')
                        if not res:
                            log.error('Authentication failed')
                            return ret
                        low.update(res)
                        low['eauth'] = self.opts['eauth']
                else:
                    user = salt.utils.get_specific_user()

                # Allocate a jid
                async_pub = self._gen_async_pub()

                if self.opts.get('runner_returns', False):
                    job_load = {
                        'jid': async_pub['jid'],
                        'tgt_type': 'runner',
                        'tgt': '',
                        'user': self.opts.get('user', ''),
                        'fun': self.opts['fun'],
                        'arg': salt.utils.args.condition_input(args, kwargs),
                    }
                else:
                    job_load = {}

                if low['fun'] == 'state.orchestrate':
                    low['kwarg']['__pub_orchestration_jid'] = async_pub['jid']

                # Run the runner!
                if self.opts.get('async', False):
                    if self.opts.get('eauth'):
                        async_pub = self.cmd_async(low)
                    else:
                        async_pub = self.async(self.opts['fun'],
                                               low,
                                               user=user,
                                               pub=async_pub)
                    # by default: info will be not enougth to be printed out !
                    log.warning('Running in async mode. Results of this execution may '
                             'be collected by attaching to the master event bus or '
                             'by examing the master job cache, if configured. '
                             'This execution is running under tag {tag}'.format(**async_pub))
                    return async_pub['jid']  # return the jid

                # otherwise run it in the main process
                if self.opts.get('eauth'):
                    ret = self.cmd_sync(low)
                    if isinstance(ret, dict) and set(ret) == set(('data', 'outputter')):
                        outputter = ret['outputter']
                        ret = ret['data']
                    else:
                        outputter = None
                    display_output(ret, outputter, self.opts)
                else:
                    ret = self._proc_function(self.opts['fun'],
                                              low,
                                              user,
                                              async_pub['tag'],
                                              async_pub['jid'],
                                              daemonize=False)
            except salt.exceptions.SaltException as exc:
                ret = '{0}'.format(exc)
                if not self.opts.get('quiet', False):
                    display_output(ret, 'nested', self.opts)
            else:
                log.debug('Runner return: {0}'.format(ret))

            if self.opts.get('runner_returns', False):
                # Save the payload to the job cache
                job_load['ret'] = ret
                save_load_func = '{0}.save_load'.format(
                    self.opts['master_job_cache'])
                self.returners[save_load_func](job_load['jid'], job_load)

            return ret
