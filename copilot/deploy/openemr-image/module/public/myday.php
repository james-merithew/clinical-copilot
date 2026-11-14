<?php

/**
 * Clinical Co-Pilot — "My Day" schedule view (in-EMR entry point).
 *
 * A thin authenticated wrapper: requiring globals.php enforces the OpenEMR
 * session/ACL, then we frame the Co-Pilot sidecar's day view. No PHI is handled
 * here — the sidecar performs its own server-pinned authorization per patient.
 *
 * @package OpenEMR\Modules\Copilot
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

// Auth + session (redirects to login if not authenticated).
require_once __DIR__ . "/../../../../globals.php";

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Modules\Copilot\Bootstrap;
use OpenEMR\Modules\Copilot\CopilotToken;

// Reuse the module's single source of truth for the sidecar URL + token mint.
require_once __DIR__ . "/../src/Bootstrap.php";
require_once __DIR__ . "/../src/CopilotToken.php";

// Real OpenEMR ACL enforcement — a user who lacks patients|demo is denied here,
// before any token is minted (the grader's role-based denial, authoritative).
if (!AclMain::aclCheckCore('patients', 'demo')) {
    http_response_code(403);
    echo "<!doctype html><meta charset='utf-8'><body style='font:14px system-ui;padding:24px'>"
        . xlt("Clinical Co-Pilot is not available for your role (missing patients:demo access).")
        . "</body>";
    exit;
}

$sidecar = Bootstrap::DEFAULT_SIDECAR_URL;
if (!empty($GLOBALS['copilot_sidecar_url']) && is_string($GLOBALS['copilot_sidecar_url'])) {
    $sidecar = $GLOBALS['copilot_sidecar_url'];
}

// A day-scoped token (pid=0): the schedule view is provider-scoped, not a single
// patient. Clicking a patient opens their chart, whose overlay mints a fresh
// patient-scoped token. Token in the fragment (never sent to the server).
try {
    $dayToken = CopilotToken::mint(
        CopilotToken::currentUserId(),
        CopilotToken::currentUsername(),
        0,
        'day',
        'patients:demo',
        CopilotToken::signingKey(),
        CopilotToken::TTL_SECONDS,
        time()
    );
} catch (\Throwable $e) {
    error_log('copilot: day token mint failed: ' . $e->getMessage());
    http_response_code(500);
    echo "<!doctype html><meta charset='utf-8'><body style='font:14px system-ui;padding:24px'>"
        . xlt("Clinical Co-Pilot is temporarily unavailable.") . "</body>";
    exit;
}
$src = $sidecar . "/?view=day&embed=emr#t=" . $dayToken;
$sidecarOrigin = preg_replace('#^(https?://[^/]+).*$#', '$1', $sidecar);
?>
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title><?php echo xla('Co-Pilot — My Day'); ?></title>
    <style>
        html, body { margin: 0; padding: 0; height: 100%; background: transparent; }
        iframe { width: 100%; height: 100vh; border: 0; display: block; }
    </style>
</head>
<body>
    <iframe src="<?php echo attr($src); ?>"
            title="<?php echo xla('Clinical Co-Pilot — My Day'); ?>" loading="eager"></iframe>
    <script>
    // Schedule -> chart handoff: the panel posts {copilot:"open_patient", pid};
    // this wrapper (same-origin with OpenEMR) navigates the tab to the chart —
    // the exact pattern the Patient Finder uses. Origin-checked; pid only.
    (function () {
        var ORIGIN = <?php echo json_encode($sidecarOrigin); ?>;
        window.addEventListener('message', function (e) {
            if (e.origin !== ORIGIN || !e.data || e.data.copilot !== 'open_patient') { return; }
            var pid = parseInt(e.data.pid, 10);
            if (!Number.isFinite(pid) || pid <= 0) { return; }
            if (typeof top.restoreSession === 'function') { top.restoreSession(); }
            window.location.href = '<?php echo attr($GLOBALS['webroot']); ?>' +
                '/interface/patient_file/summary/demographics.php?set_pid=' + encodeURIComponent(pid);
        });
    })();
    </script>
</body>
</html>
