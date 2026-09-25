// Direct Phoenix protocol probe. Credentials arrive on stdin and are never logged.
const input = JSON.parse(await new Promise((resolve) => {
  let data = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => { data += chunk; });
  process.stdin.on('end', () => resolve(data));
}));

const socketUrl = `${input.apiUrl.replace(/^http/, 'ws')}/realtime/v1/websocket?apikey=${encodeURIComponent(input.apikey)}&vsn=1.0.0`;
const sockets = [];

function waitFor(socket, predicate, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      socket.removeEventListener('message', onMessage);
      reject(new Error('Realtime response timed out'));
    }, timeoutMs);
    function onMessage(event) {
      const message = JSON.parse(event.data);
      if (!predicate(message)) return;
      clearTimeout(timeout);
      socket.removeEventListener('message', onMessage);
      resolve(message);
    }
    socket.addEventListener('message', onMessage);
  });
}

async function join(ref, token, isPrivate) {
  const socket = new WebSocket(socketUrl);
  sockets.push(socket);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', () => reject(new Error('Realtime socket failed')), { once: true });
  });
  const reply = waitFor(socket, (message) => message.event === 'phx_reply' && message.ref === ref);
  socket.send(JSON.stringify({
    topic: `realtime:${input.topic}`,
    event: 'phx_join', ref, join_ref: ref,
    payload: {
      access_token: token,
      config: { private: isPrivate, broadcast: { ack: true, self: isPrivate }, presence: { enabled: false } },
    },
  }));
  const result = await reply;
  return { socket, status: result.payload.status };
}

try {
  const isPrivate = input.mode === 'private';
  const a = await join('1', isPrivate ? input.tokenA : input.anonKey, isPrivate);
  const b = await join('2', isPrivate ? input.tokenB : input.anonKey, isPrivate);
  const result = { a: a.status, b: b.status };
  if (isPrivate) {
    result.anon = (await join('4', input.anonKey, true)).status;
  }
  if (a.status === 'ok' && (isPrivate || b.status === 'ok')) {
    const receiver = isPrivate ? a.socket : b.socket;
    const broadcast = waitFor(receiver, (message) => message.event === 'broadcast' && message.payload?.event === 'lab-canary');
    a.socket.send(JSON.stringify({
      topic: `realtime:${input.topic}`,
      event: 'broadcast', ref: '3', join_ref: '1',
      payload: { type: 'broadcast', event: 'lab-canary', payload: { marker: 'visible' } },
    }));
    result.delivered = (await broadcast).payload.payload.marker === 'visible';
  }
  process.stdout.write(JSON.stringify(result));
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
} finally {
  for (const socket of sockets) socket.close();
}
