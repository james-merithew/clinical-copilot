// k6 load/stress test for the Clinical Co-Pilot sidecar POST /chat.
//
// Measures app+DB infrastructure latency and throughput. Point it at the
// MOCK-LLM server (default host.docker.internal:8809) so there is no token
// spend and the LLM latency is stubbed out — the numbers reflect the FastAPI
// app + Authorization Gateway + read-only MariaDB path only.
//
// Run (from copilot/load/), mock server, 10 then 50 VUs baked in as stages:
//   docker run --rm -i --add-host=host.docker.internal:host-gateway \
//     grafana/k6 run - <chat_load.js
//
// Override target / auth / load shape via env:
//   -e BASE_URL=http://host.docker.internal:8809
//   -e KEY=<signing key>             (the copilot_signing_key.secret the sidecar
//                                      verifies against; get it from the DB, or
//                                      use the dev key under COPILOT_AUTH_DEV_MODE)
//   -e SUB=1                          (OpenEMR users.id to mint tokens for)
//   -e VUS=10 -e DURATION=30s        (single fixed-VU run instead of stages)
//   -e SCENARIO=stages|constant
//
// The sidecar binds each session token to ONE patient, so this script mints a
// per-pid patient-scoped token in-process (HMAC-SHA256 over the base64url
// payload, exactly AUTH_TOKEN_SPEC.md) from -e KEY. No reusable bearer.
//
import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import crypto from 'k6/crypto';
import encoding from 'k6/encoding';

const BASE_URL = __ENV.BASE_URL || 'http://host.docker.internal:8809';
const KEY = __ENV.KEY || 'dev-local-signing-key-not-for-prod';
const SUB = parseInt(__ENV.SUB || '1', 10);
const TTL = parseInt(__ENV.TTL || '900', 10);
const PIDS = [1, 101, 102]; // authorized patient ids
const MESSAGES = ['brief me', 'what changed', 'summarize recent labs'];

// Canonical (sorted-key, compact) JSON so the payload matches the mint on both
// language sides; verification never depends on it, but this keeps mint↔mint
// byte-identical. Build the string by hand in sorted key order.
function mintToken(sub, pid, scope, acl) {
  const now = Math.floor(Date.now() / 1000);
  const payload =
    '{"acl":' + JSON.stringify(acl) +
    ',"exp":' + (now + TTL) +
    ',"iat":' + now +
    ',"pid":' + pid +
    ',"scope":' + JSON.stringify(scope) +
    ',"sub":' + sub +
    ',"usr":"load"' +
    ',"v":1}';
  const payloadB64 = encoding.b64encode(payload, 'rawurl');
  const sig = crypto.hmac('sha256', KEY, payloadB64, 'base64rawurl');
  return payloadB64 + '.' + sig;
}

const TOKENS = {};
for (const pid of PIDS) TOKENS[pid] = mintToken(SUB, pid, 'patient', 'patients:demo');

const errorRate = new Rate('chat_errors');
const chatLatency = new Trend('chat_latency_ms', true);

// Two ways to run:
//  - SCENARIO=constant with VUS/DURATION  -> a single fixed-VU level (used by
//    the driver script to run 10-VU and 50-VU passes separately for clean
//    per-level percentile tables).
//  - SCENARIO=stages (default)            -> 10 VUs for 30s then 50 VUs for 30s
//    in one invocation.
const SCENARIO = __ENV.SCENARIO || 'stages';
const VUS = parseInt(__ENV.VUS || '10', 10);
const DURATION = __ENV.DURATION || '30s';

let options = {
  discardResponseBodies: false,
  // emit p50/p95/p99 (k6's default summary omits p99)
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  thresholds: {
    chat_errors: ['rate<0.05'],
  },
};

if (SCENARIO === 'constant') {
  options.scenarios = {
    fixed: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
    },
  };
} else {
  options.scenarios = {
    ramp: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { target: 10, duration: '3s' },
        { target: 10, duration: '30s' },
        { target: 50, duration: '3s' },
        { target: 50, duration: '30s' },
        { target: 0, duration: '2s' },
      ],
      gracefulRampDown: '2s',
    },
  };
}

export { options };

export default function () {
  const pid = PIDS[Math.floor(Math.random() * PIDS.length)];
  const message = MESSAGES[Math.floor(Math.random() * MESSAGES.length)];
  const payload = JSON.stringify({ pid: pid, message: message });
  const params = {
    headers: {
      'Authorization': `Bearer ${TOKENS[pid]}`,
      'Content-Type': 'application/json',
    },
    timeout: '30s',
  };

  const res = http.post(`${BASE_URL}/chat`, payload, params);

  const ok = check(res, {
    'status is 200': (r) => r.status === 200,
    'has correlation_id': (r) => {
      try {
        return JSON.parse(r.body).correlation_id !== undefined;
      } catch (e) {
        return false;
      }
    },
  });

  errorRate.add(!ok);
  chatLatency.add(res.timings.duration);
}
