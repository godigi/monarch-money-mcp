"""Private tool logging and bounded anonymous OAuth admission."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path


class PrivateValidationFilter(logging.Filter):
    """Framework validation dictionaries include raw input, contexts and signed URLs."""
    def filter(self, record):
        if isinstance(record.msg,str) and record.msg.startswith('Invalid arguments for tool'):
            # Only retain our registered tool name; no arbitrary error dictionary or repr.
            name = record.args[0] if isinstance(record.args,tuple) and record.args else 'unknown'
            record.msg = 'Invalid arguments for tool %r; input details redacted'
            record.args = (name,)
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def install_private_logging():
    logger=logging.getLogger('fastmcp.server.server')
    if not any(isinstance(f,PrivateValidationFilter) for f in logger.filters):
        logger.addFilter(PrivateValidationFilter())


class OAuthAdmissionGuard:
    """Bound public OAuth state allocation; existing token/MCP traffic bypasses admission.

    The service uses one process. Rates are process-local, but persistent state quotas
    survive restarts. Quotas reject new flows without deleting existing OAuth state.
    """
    MAX_BODY=16*1024
    MAX_STATE=8*1024*1024
    MAX_FILES=4096
    MAX_CLIENTS=128
    RESERVED_BYTES=64*1024
    PATHS={'/register','/authorize','/consent'}
    BODY_PATHS=PATHS|{'/token','/revoke','/auth/callback'}
    BODY_TIMEOUT=5

    def __init__(self,app,*,storage_root:Path,clock=time.monotonic):
        self.app=app
        self.storage_root=Path(storage_root)
        self.clock=clock
        self._events=deque()
        self._lock=asyncio.Lock()

    def storage_available(self,*,registration):
        if not self.storage_root.exists():return True
        size=count=clients=0
        # Follow no symlinks and stop at quota; storage content is never read/logged.
        pending=[self.storage_root]
        while pending:
            path=pending.pop()
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_symlink():return False
                    if entry.is_dir(follow_symlinks=False):pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        count+=1;size+=entry.stat(follow_symlinks=False).st_size
                        if path.name.startswith('S_mcp_oauth_proxy_clients-'):clients+=1
                    if count>=self.MAX_FILES or size+self.RESERVED_BYTES>self.MAX_STATE:
                        return False
                    if registration and clients>=self.MAX_CLIENTS:return False
        return True

    async def reject(self,send,status,message,retry=None):
        headers=[(b'content-type',b'application/json'),(b'cache-control',b'no-store')]
        if retry is not None:headers.append((b'retry-after',str(retry).encode()))
        body=json.dumps({'error':'temporarily_unavailable' if status in (429,503) else 'invalid_client_metadata','error_description':message}).encode()
        await send({'type':'http.response.start','status':status,'headers':headers})
        await send({'type':'http.response.body','body':body})

    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('path','').rstrip('/') not in self.BODY_PATHS:
            return await self.app(scope,receive,send)
        if len(scope.get('query_string',b''))>self.MAX_BODY:
            return await self.reject(send,413,'OAuth request is too large')
        lengths=[v for k,v in scope.get('headers',[]) if k.lower()==b'content-length']
        if lengths:
            try:
                declared=int(lengths[0])
                if len(lengths)!=1 or declared<0:raise ValueError()
            except ValueError:return await self.reject(send,400,'Invalid request length')
            if declared>self.MAX_BODY:return await self.reject(send,413,'OAuth request is too large')
        admission=scope.get('path','').rstrip('/') in self.PATHS
        if admission:
            async with self._lock:
                now=self.clock()
                while self._events and self._events[0]<=now-3600:self._events.popleft()
                if len(self._events)>=120 or sum(t>now-60 for t in self._events)>=20:
                    return await self.reject(send,429,'Too many new OAuth requests; retry later',retry=60)
                self._events.append(now)
        body=bytearray()
        try:
            async with asyncio.timeout(self.BODY_TIMEOUT):
                while True:
                    frame=await receive()
                    if frame['type']=='http.disconnect':return
                    if frame['type']!='http.request':continue
                    chunk=frame.get('body',b'')
                    if len(body)+len(chunk)>self.MAX_BODY:
                        return await self.reject(send,413,'OAuth request is too large')
                    body.extend(chunk)
                    if not frame.get('more_body',False):break
        except TimeoutError:
            return await self.reject(send,408,'OAuth request body timed out')
        registration=scope.get('path','').rstrip('/')=='/register'
        if registration and scope.get('method')=='POST':
            try:
                metadata=json.loads(body)
                if not isinstance(metadata,dict):raise ValueError()
                name=metadata.get('client_name')
                if name is not None and (not isinstance(name,str) or len(name)>256):raise ValueError()
            except (ValueError,UnicodeError):
                return await self.reject(send,400,'Invalid or oversized client metadata')
        delivered=False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {'type':'http.request','body':bytes(body),'more_body':False}
            return await receive()
        if not admission:
            return await self.app(scope,replay,send)
        async with self._lock:
            try:available=await asyncio.to_thread(self.storage_available,registration=registration)
            except OSError:available=False
            if not available:return await self.reject(send,503,'New OAuth flows temporarily unavailable',retry=300)
            await self.app(scope,replay,send)
