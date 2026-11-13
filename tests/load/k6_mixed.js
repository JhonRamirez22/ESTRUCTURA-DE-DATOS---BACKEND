// Carga mixta realista contra la API local. Uso:
//   k6 run -e BASE=http://127.0.0.1:8100 -e VUS=200 -e DURATION=60s tests/load/k6_mixed.js
// Mezcla: 70 % repartidores (login una vez, GPS cada ciclo, su ruta), 15 % despachadores
// (listados), 15 % anónimos (seguimiento por guía y solicitudes públicas).
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const BASE = __ENV.BASE || 'http://127.0.0.1:8100';
const VUS = parseInt(__ENV.VUS || '200');
const PASSWORD = __ENV.LOAD_PASSWORD || 'Carga-Masiva-2026-Zz1';
const ORIGIN = __ENV.ORIGIN || 'http://localhost:3000';
const COURIERS = parseInt(__ENV.COURIERS || '1000');
const DISPATCHERS = parseInt(__ENV.DISPATCHERS || '20');

const serverErrors = new Counter('server_errors_5xx');
const shed = new Counter('shed_503_con_retry_after');
const designed = new Counter('bandeja_llena_503');
const loginTime = new Trend('login_ms', true);
const readTime = new Trend('read_ms', true);

export const options = {
  noCookiesReset: true, // la sesión vive entre iteraciones, como un navegador real
  scenarios: {
    mixed: {
      executor: 'ramping-vus',
      stages: [
        { duration: __ENV.RAMP || '15s', target: VUS },
        { duration: __ENV.DURATION || '60s', target: VUS },
        { duration: '10s', target: 0 },
      ],
      gracefulRampDown: '10s',
    },
  },
  thresholds: {
    server_errors_5xx: ['count==0'],
    'http_req_duration{kind:read}': ['p(95)<500'],
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

function jsonHeaders(extra) {
  return Object.assign({ 'Content-Type': 'application/json', Origin: ORIGIN }, extra || {});
}

// 5xx reales: se excluyen las respuestas de diseño (bandeja de solicitudes llena y descarte de
// carga con Retry-After), que son contrapresión y no fallos.
function track(res) {
  if (res.status < 500) return;
  if (res.status === 503 && res.url.indexOf('/solicitudes') >= 0) designed.add(1);
  else if (res.status === 503 && res.headers['Retry-After']) shed.add(1);
  else serverErrors.add(1);
}

function login(email) {
  const res = http.post(`${BASE}/api/v1/auth/login`, JSON.stringify({ email, password: PASSWORD }), {
    headers: jsonHeaders(), tags: { kind: 'login' },
  });
  track(res);
  loginTime.add(res.timings.duration);
  check(res, { 'login 200': (r) => r.status === 200 });
  return res.status === 200 ? res.headers['X-Csrf-Token'] : null;
}

let session = null; // por VU

export default function () {
  const role = __VU % 100;
  if (role < 70) courier();
  else if (role < 85) dispatcher();
  else anonymous();
}

function courier() {
  if (!session) {
    const n = (__VU * 7) % COURIERS;
    const csrf = login(`c${n}@carga.co`);
    if (!csrf) { sleep(2); return; }
    session = { csrf, n };
  }
  const now = Date.now();
  if (now - (session.lastGps || 0) >= 5000) {
  session.lastGps = now;
  const gps = http.post(`${BASE}/api/v1/repartidores/me/ubicacion`,
    JSON.stringify({ lat: 1.2136 + Math.random() * 1e-3, lng: -77.2811 + Math.random() * 1e-3 }),
    { headers: jsonHeaders({ 'X-CSRF-Token': session.csrf }), tags: { kind: 'write' } });
  track(gps);
  check(gps, { 'gps ok o limitada': (r) => r.status === 201 || r.status === 429 });
  }
  const me = http.get(`${BASE}/api/v1/repartidores/me`, { tags: { kind: 'read' } });
  track(me); readTime.add(me.timings.duration);
  const routes = http.get(`${BASE}/api/v1/rutas`, { tags: { kind: 'read' } });
  track(routes); readTime.add(routes.timings.duration);
  check(routes, { 'rutas 200': (r) => r.status === 200 });
  sleep(2 + Math.random() * 3);
}

function dispatcher() {
  if (!session) {
    const csrf = login(`d${__VU % DISPATCHERS}@carga.co`);
    if (!csrf) { sleep(2); return; }
    session = { csrf };
  }
  for (const path of ['/api/v1/pedidos', '/api/v1/rutas', '/api/v1/repartidores']) {
    const res = http.get(`${BASE}${path}`, { tags: { kind: 'read' } });
    track(res); readTime.add(res.timings.duration);
    check(res, { 'lectura 200 o limitada': (r) => r.status === 200 || r.status === 429 });
  }
  sleep(3 + Math.random() * 4);
}

function anonymous() {
  const n = Math.floor(Math.random() * COURIERS);
  const res = http.get(`${BASE}/api/v1/seguimiento?guia=guia-carga-${n}-${Math.floor(Math.random() * 5)}`,
    { headers: { 'X-Forwarded-For': `198.51.100.${(__VU % 250) + 1}` }, tags: { kind: 'read' } });
  track(res); readTime.add(res.timings.duration);
  check(res, { 'seguimiento 200 o limitado': (r) => r.status === 200 || r.status === 429 });
  const req = http.post(`${BASE}/api/v1/solicitudes`,
    JSON.stringify({ address: `Calle 18 # 22-${__VU}`, lat: 1.2136, lng: -77.2811 }),
    { headers: jsonHeaders(), tags: { kind: 'write' } });
  track(req);
  check(req, { 'solicitud creada o limitada': (r) => [201, 429, 503].includes(r.status) });
  sleep(2 + Math.random() * 3);
}
