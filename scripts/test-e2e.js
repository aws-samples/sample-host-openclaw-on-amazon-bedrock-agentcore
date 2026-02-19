#!/usr/bin/env node
const WebSocket = require('ws');
const crypto = require('crypto');

const CF_URL = process.env.CF_URL;
if (!CF_URL) { console.error('CF_URL env var required (e.g. wss://XXXX.cloudfront.net/)'); process.exit(1); }
const token = process.env.GATEWAY_TOKEN;
if (!token) { console.error('GATEWAY_TOKEN env var required'); process.exit(1); }

const origin = CF_URL.replace('wss://', 'https://').replace(/\/$/, '');
const ws = new WebSocket(CF_URL, {
  headers: { Origin: origin }
});
let done = false;
let reqId = 0;
let responseBuffer = '';

function nextId() { return String(++reqId); }

ws.on('open', () => console.log('Connected to', CF_URL));

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());

  if (msg.type === 'event' && msg.event === 'connect.challenge') {
    console.log('Got challenge, sending connect request...');

    const connectReq = {
      type: 'req',
      id: nextId(),
      method: 'connect',
      params: {
        minProtocol: 3,
        maxProtocol: 3,
        client: {
          id: 'openclaw-control-ui',
          version: 'dev',
          platform: 'linux',
          mode: 'webchat',
          instanceId: crypto.randomUUID()
        },
        role: 'operator',
        scopes: ['operator.admin'],
        auth: { token: token },
        caps: [],
        userAgent: 'test-client/1.0',
        locale: 'en'
      }
    };
    ws.send(JSON.stringify(connectReq));
    return;
  }

  if (msg.type === 'res') {
    if (msg.id === '1') {
      // connect response
      if (msg.ok) {
        console.log('Authenticated!');
        console.log('Sending chat message...');
        const chatReq = {
          type: 'req',
          id: nextId(),
          method: 'chat.send',
          params: {
            message: 'Say hi in 3 words.',
            sessionKey: 'test-' + Date.now(),
            idempotencyKey: crypto.randomUUID()
          }
        };
        ws.send(JSON.stringify(chatReq));
      } else {
        console.log('Connect failed:', JSON.stringify(msg.error));
        done = true;
        ws.close();
      }
    } else if (!msg.ok) {
      console.log('Request', msg.id, 'failed:', JSON.stringify(msg.error));
    } else {
      console.log('Request', msg.id, 'ok');
    }
    return;
  }

  if (msg.type === 'event') {
    const ev = msg.event || '';
    const payload = msg.payload || {};

    if (ev === 'agent' && payload.stream === 'assistant' && payload.data?.delta) {
      // Streaming text delta from the model
      process.stdout.write(payload.data.delta);
      responseBuffer += payload.data.delta;
    } else if (ev === 'chat' && payload.state === 'final') {
      // Chat turn complete — extract final text
      const finalText = payload.message?.content?.[0]?.text || '';
      console.log('\n\nChat complete! Final text:', finalText);
      done = true;
      ws.close();
    } else if (ev === 'agent' && payload.stream === 'lifecycle') {
      console.log('Agent', payload.data?.phase);
    } else if (ev !== 'health' && ev !== 'tick' && ev !== 'chat' && ev !== 'presence') {
      console.log('Event:', ev, JSON.stringify(payload).substring(0, 150));
    }
  }
});

ws.on('error', (err) => { console.error('WS Error:', err.message); process.exit(1); });
ws.on('close', (code, reason) => {
  console.log('Connection closed:', code, reason.toString());
  if (responseBuffer) console.log('Full response:', responseBuffer);
  process.exit(done ? 0 : 1);
});

setTimeout(() => {
  console.log('\nTimeout after 45s');
  if (responseBuffer) console.log('Partial response:', responseBuffer);
  ws.close();
  process.exit(1);
}, 45000);
