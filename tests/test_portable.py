import hashlib
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

prepare = module('prepare_gateway_ca', ROOT / 'prepare_gateway_ca.py')
bootstrap = module('tls_bootstrap', ROOT / 'google_adk_agent/tls_bootstrap.py')

class PortableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with zipfile.ZipFile('/home/user/output/gateway_ca_bootstrap_replacements.zip') as z:
            cls.pem = z.read('google_adk_agent/gateway-root.pem').decode('ascii')
        cls.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cls.pem)).hexdigest().upper()

    def test_prepare_and_bootstrap(self):
        with tempfile.TemporaryDirectory() as d:
            agent = Path(d)
            (agent/'agent.py').write_text('')
            payload = {'name': 'projects/p/locations/r/agentGateways/g',
                       'googleManaged': {'governedAccessPath': 'AGENT_TO_ANYWHERE'},
                       'agentGatewayCard': {'rootCertificates': [self.pem]}}
            with patch.object(prepare.subprocess, 'check_output', return_value=json.dumps(payload)):
                prepare.prepare('p', 'r', 'g', self.fingerprint, agent)
            old = {k: os.environ.get(k) for k in (*bootstrap._ENV_NAMES, bootstrap.FINGERPRINT_ENV)}
            old_cert = bootstrap._CERT
            try:
                bootstrap._CERT = agent/'gateway-root.pem'
                os.environ[bootstrap.FINGERPRINT_ENV] = self.fingerprint
                os.environ.pop('SSL_CERT_FILE', None)
                combined = bootstrap.bootstrap_gateway_ca()
                try:
                    self.assertIn(self.fingerprint, {hashlib.sha256(c).hexdigest().upper()
                        for c in ssl.create_default_context().get_ca_certs(binary_form=True)})
                    self.assertGreater(len(ssl.create_default_context().get_ca_certs()), 1)
                finally:
                    combined.unlink()
            finally:
                bootstrap._CERT = old_cert
                for k,v in old.items():
                    if v is None: os.environ.pop(k, None)
                    else: os.environ[k] = v

    def test_wrong_approval_does_not_stage(self):
        with tempfile.TemporaryDirectory() as d:
            agent = Path(d)
            (agent/'agent.py').write_text('')
            payload = {'name': 'projects/p/locations/r/agentGateways/g',
                       'googleManaged': {'governedAccessPath': 'AGENT_TO_ANYWHERE'},
                       'agentGatewayCard': {'rootCertificates': [self.pem]}}
            with patch.object(prepare.subprocess, 'check_output', return_value=json.dumps(payload)):
                with self.assertRaisesRegex(ValueError, 'differs'):
                    prepare.prepare('p','r','g','0'*64,agent)
            self.assertFalse((agent/'gateway-root.pem').exists())

    def test_standard_redeploy_does_not_fetch_ca(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'google_adk_agent').mkdir()
            (root/'google_adk_agent/agent.py').write_text('')
            (root/'google_adk_agent/gateway-root.pem').write_text(self.pem)
            (root/'runtime.env').write_text('MONGODB_LOG_DIAG_GATEWAY_CA_SHA256='+self.fingerprint+'\n')
            (root/'adk').write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGS_OUT"\n')
            (root/'adk').chmod(0o755)
            env={**os.environ,'PROJECT':'p','REGION':'r','ENGINE_ID':'e','DISPLAY_NAME':'d',
                 'ENV_FILE':str(root/'runtime.env'),'PATH':f'{root}:{os.environ["PATH"]}',
                 'ARGS_OUT':str(root/'args.txt')}
            subprocess.run(['sh',str(ROOT/'redeploy_agent.sh')],cwd=root,env=env,check=True)
            args=(root/'args.txt').read_text()
            for expected in ['deploy\nagent_engine','--project=p','--region=r','--agent_engine_id=e',
                             '--env_file='+str(root/'runtime.env')]: self.assertIn(expected,args)
            self.assertNotIn('gcloud',args)
            self.assertTrue((root/'google_adk_agent/gateway-root.pem').exists())

if __name__=='__main__': unittest.main()
