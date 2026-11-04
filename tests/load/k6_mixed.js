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
