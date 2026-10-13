"""Security regressions use fabricated inputs and never contact Monarch."""
import asyncio
import base64
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_security import server
try:
    from security import OAuthAdmissionGuard
except ImportError:
    OAuthAdmissionGuard = None


class PrivateLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_remote_validation_never_logs_receipt_or_signed_url(self):
        marker = 'SYNTHETIC_PRIVATE_MARKER'
        encoded = base64.b64encode((marker*50).encode()).decode()
        url = 'https://files.example.com/receipt?signature='+marker
        log = logging.getLogger('fastmcp.server.server')
        stream = io.StringIO(); handler = logging.StreamHandler(stream); log.addHandler(handler)
        try:
            for name,args in [
                ('upload_transaction_receipt_bytes',{'transaction_id':'synthetic-txn','data_base64':encoded}),
                ('upload_transaction_receipt',{'transaction_id':'synthetic-txn','file':{'download_url':url}}),
                ('preview_transaction_rule_update',{'rule_id':'synthetic-rule','changes':{'category_id':'synthetic','secret_extra':marker}}),
            ]:
                with self.assertRaises(Exception): await server.mcp.call_tool(name,args)
            output=stream.getvalue()
            self.assertNotIn(marker,output)
            self.assertNotIn(encoded,output)
            self.assertNotIn(url,output)
            self.assertIn('Invalid arguments',output)
            self.assertIn('upload_transaction_receipt_bytes',output)
        finally: log.removeHandler(handler)

    async def test_token_client_lookup_cannot_create_anonymous_cimd_state(self):
        from starlette.testclient import TestClient
        from fastmcp.server.auth.cimd import CIMDClientManager
        probe=AsyncMock(return_value=None)
        root=server.fastmcp_settings.home/'oauth-proxy'
        # FileTreeStore initializes fixed collection metadata on its first lookup.
        await server._AUTH.get_client('unregistered-synthetic')
        before={str(p) for p in root.rglob('*') if p.is_file()}
        with patch.object(CIMDClientManager,'get_client',probe):
            with TestClient(server.app) as client:
                response=client.post('/token',data={
                    'client_id':'https://cimd.example.test/metadata.json',
                    'grant_type':'authorization_code','code':'invalid-synthetic-code',
                    'code_verifier':'S'*43,'redirect_uri':'http://localhost:9911/callback'})
        self.assertIn(response.status_code,(400,401))
        self.assertEqual(probe.await_count,0)
        self.assertEqual({str(p) for p in root.rglob('*') if p.is_file()},before)

    async def test_local_helper_validation_redacts_inputs_too(self):
        from scripts.local_receipt_mcp import build_server
        class Bridge:
            async def upload_transaction_receipt(self, transaction_id: str, file_path: str):
                raise AssertionError('Validation should reject before file read')
        app=build_server(Bridge())
        log=logging.getLogger('fastmcp.server.server')
        stream=io.StringIO();handler=logging.StreamHandler(stream);log.addHandler(handler)
        marker='SYNTHETIC_PRIVATE_PATH'
        try:
            with self.assertRaises(Exception):
                await app.call_tool('upload_transaction_receipt',{'file_path':'/private/'+marker})
            self.assertNotIn(marker,stream.getvalue())
            self.assertIn('Invalid arguments',stream.getvalue())
        finally:log.removeHandler(handler)

    async def test_legacy_upstream_failure_is_sanitized_without_retry(self):
        marker='SYNTHETIC_UPSTREAM_PRIVATE_VALUE'
        call=AsyncMock(side_effect=RuntimeError(marker))
        with self.assertRaises(ValueError) as caught:
            await server._call(call)
        self.assertNotIn(marker,str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(call.await_count,1)

    async def test_transaction_limit_refuses_unbounded_fetch(self):
        backend=AsyncMock(return_value={})
        with patch.object(server.mm,'get_transactions',backend):
            for limit in (-1,0,501,1000000,True):
                with self.assertRaises(ValueError):await server.get_transactions(limit=limit)
            self.assertEqual(backend.await_count,0)
            await server.get_transactions(limit=500)
            self.assertEqual(backend.await_args.kwargs['limit'],500)

    async def test_budget_refuses_nonfinite_amounts_and_non_month_start_dates(self):
        backend=AsyncMock(return_value={})
        with patch.object(server.mm,'set_budget_amount',backend):
            for amount,day in [(float('nan'),'2026-10-01'),(float('inf'),'2026-10-01'),(-1,'2026-10-01'),(10,'garbage'),(10,'2026-10-02')]:
                with self.assertRaises(ValueError):await server.set_budget_amount(amount,'cat1',day)
            self.assertEqual(backend.await_count,0)
            await server.set_budget_amount(10,'cat1','2026-10-01')
            self.assertEqual(backend.await_count,1)


class OAuthAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(OAuthAdmissionGuard,'OAuth admission guard missing')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.clock=100.0;self.calls=[]
        async def downstream(scope,receive,send):
            frame=await receive();self.calls.append(frame.get('body',b''))
            await send({'type':'http.response.start','status':201,'headers':[]})
            await send({'type':'http.response.body','body':b'{}'})
        self.guard=OAuthAdmissionGuard(downstream,storage_root=Path(self.temp.name),clock=lambda:self.clock)

    async def request(self,body=b'{}',path='/register',headers=None,chunks=None):
        frames=[{'type':'http.request','body':c,'more_body':i<len(chunks)-1} for i,c in enumerate(chunks)] if chunks else [{'type':'http.request','body':body,'more_body':False}]
        output=[];reads=0
        async def receive():
            nonlocal reads
            reads+=1
            return frames.pop(0) if frames else {'type':'http.disconnect'}
        async def send(frame):output.append(frame)
        await self.guard({'type':'http','method':'POST','path':path,'query_string':b'','headers':headers or []},receive,send)
        return next(f['status'] for f in output if f['type']=='http.response.start'),reads,output

    async def test_large_declared_body_rejected_without_read(self):
        status,reads,_=await self.request(headers=[(b'content-length',b'200000')])
        self.assertEqual(status,413);self.assertEqual(reads,0);self.assertEqual(self.calls,[])

    async def test_chunked_body_bound_and_metadata_name_bound(self):
        status,_,_=await self.request(chunks=[b'x'*9000,b'y'*9000])
        self.assertEqual(status,413);self.assertEqual(self.calls,[])
        status,_,_=await self.request(body=json.dumps({'client_name':'x'*257}).encode())
        self.assertEqual(status,400);self.assertEqual(self.calls,[])

    async def test_legitimate_dcr_preserved_and_mcp_receipts_not_capped(self):
        body=json.dumps({'client_name':'ChatGPT','redirect_uris':['https://chatgpt.com/connector/oauth/R3V1cbYg3KfX']}).encode()
        status,_,_=await self.request(body=body)
        self.assertEqual(status,201);self.assertEqual(self.calls[-1],body)
        status,_,_=await self.request(body=b'x'*20000,path='/mcp')
        self.assertEqual(status,201);self.assertEqual(len(self.calls[-1]),20000)

    async def test_global_rate_limit_and_expiry(self):
        for _ in range(20):self.assertEqual((await self.request())[0],201)
        status,_,output=await self.request()
        self.assertEqual(status,429)
        self.assertTrue(any(b'retry-after'==k for f in output for k,v in f.get('headers',[])))
        self.clock+=61
        self.assertEqual((await self.request())[0],201)

    async def test_oauth_token_and_revoke_bodies_also_have_size_bound(self):
        for path in ('/token','/revoke','/auth/callback'):
            status,reads,_=await self.request(path=path,headers=[(b'content-length',b'200000')])
            self.assertEqual(status,413);self.assertEqual(reads,0)

    async def test_slow_oauth_body_has_total_deadline(self):
        self.guard.BODY_TIMEOUT=0.01
        output=[]
        async def slow_receive():
            await asyncio.sleep(1)
            return {'type':'http.request','body':b'{}','more_body':False}
        async def send(frame):output.append(frame)
        await self.guard({'type':'http','method':'POST','path':'/register','query_string':b'','headers':[]},slow_receive,send)
        self.assertEqual(next(f['status'] for f in output if f['type']=='http.response.start'),408)
        self.assertEqual(self.calls,[])

    async def test_persistent_storage_quota_survives_new_guard(self):
        p=Path(self.temp.name)/'filled';p.write_bytes(b'x'*(8*1024*1024))
        status,_,_=await self.request()
        self.assertEqual(status,503);self.assertEqual(self.calls,[])
        status,_,_=await self.request(body=b'{}',path='/mcp')
        self.assertEqual(status,201)

    async def test_client_count_quota_and_new_authorization_storage_block(self):
        p=Path(self.temp.name)/'fingerprint'/'S_mcp_oauth_proxy_clients-TEST';p.mkdir(parents=True)
        for i in range(128):(p/str(i)).write_text('{}')
        status,_,_=await self.request()
        self.assertEqual(status,503);self.assertEqual(self.calls,[])
        # Full storage budget protects anonymous authorization state creation too.
        (Path(self.temp.name)/'filled').write_bytes(b'x'*(8*1024*1024))
        status,_,_=await self.request(path='/authorize')
        self.assertEqual(status,503)

if __name__=='__main__':unittest.main()
