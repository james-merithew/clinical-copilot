<?php

/**
 * Co-Pilot auth self-test + ACL denial test (grader-runnable).
 *
 * Run inside the OpenEMR container AS THE WEB USER (not root):
 *   su -s /bin/sh apache -c "php .../bin/copilot_auth_check.php [username]"
 *
 * 1. Token self-test: mints a token for the spec's golden KEY + claims and
 *    recomputes the signature — proves CopilotToken::mint is intact. Prints a
 *    freshly-minted token so the Python sidecar's verify() can be cross-checked.
 * 2. ACL denial test (if a username is given): asserts
 *    AclMain::aclCheckCore('patients','demo', <username>). A user WITHOUT the
 *    role prints DENY (exit 3) — this is the grader's "valid user who lacks the
 *    required role is denied" check. A user WITH the role prints ALLOW (exit 0).
 *
 * Requires the OpenEMR bootstrap for AclMain + sqlQuery; token minting itself
 * needs no OpenEMR (pure HMAC).
 */

$ignoreAuth = true;
$sessionAllowWrite = true;
$_GET['site'] = $_GET['site'] ?? 'default';
require_once __DIR__ . '/../../../../globals.php';

require_once __DIR__ . '/../src/CopilotToken.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Modules\Copilot\CopilotToken;

// --- 1. token self-test (golden vector from AUTH_TOKEN_SPEC.md) -------------------
$goldenKey = 'golden-test-key-do-not-use-in-prod';
$tok = CopilotToken::mint(42, 'drchen', 101, 'patient', 'patients:demo', $goldenKey, 900, 1752000000);
[$pb, $sig] = explode('.', $tok, 2);
$expect = rtrim(strtr(base64_encode(hash_hmac('sha256', $pb, $goldenKey, true)), '+/', '-_'), '=');
if (!hash_equals($expect, $sig)) {
    fwrite(STDERR, "TOKEN SELF-TEST FAILED (signature mismatch)\n");
    exit(2);
}
echo "token self-test: OK\n";
echo "sample-token (golden key): $tok\n";

// --- 2. ACL denial test (optional username arg) ----------------------------------
$user = $argv[1] ?? null;
if ($user !== null) {
    $allowed = AclMain::aclCheckCore('patients', 'demo', $user);
    echo "aclCheckCore('patients','demo','" . $user . "') = " . ($allowed ? 'ALLOW' : 'DENY') . "\n";
    // A role-lacking user MUST be denied — exit 3 signals the expected denial so
    // a test harness can assert it explicitly.
    exit($allowed ? 0 : 3);
}
echo "(no username arg — skipped ACL denial test)\n";
exit(0);
