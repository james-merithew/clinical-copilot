<?php

/**
 * Bootstrap for the Clinical Co-Pilot module.
 *
 * Two integration points, both via OpenEMR's native event system (no core edits):
 *   1. An in-chart card — injected at the TOP of the patient demographics page so
 *      it is the first thing the physician sees on chart-open, auto-scoped to the
 *      currently open patient via $event->getPid(). Wrapped in OpenEMR's native
 *      collapsible card widget so it looks and behaves like a built-in panel.
 *   2. A top-level "Co-Pilot" main-menu item → the module's My Day page, giving
 *      the physician the multi-patient schedule view without opening a chart.
 *
 * Lifecycle (deliberate): the card lives in the chart page DOM, so a fresh,
 * patient-scoped instance is created on every chart-open and torn down on
 * navigate-away. The pid is server-rendered into the iframe src, so it can never
 * drift from the open chart. The conversation is per-chart-open and stateless —
 * no PHI is retained across patients.
 *
 * Ships no install SQL and no ACL tables.
 *
 * @package OpenEMR\Modules\Copilot
 * @link    https://www.open-emr.org
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Events\PatientDemographics\RenderEvent;
use OpenEMR\Menu\MenuEvent;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

class Bootstrap
{
    const MODULE_NAME = "oe-module-copilot";

    /**
     * Default sidecar base URL. Overridable via $GLOBALS['copilot_sidecar_url'].
     * Change here to repoint the module at a different deployment.
     */
    const DEFAULT_SIDECAR_URL = "https://copilot-production-5e86.up.railway.app";

    public function __construct(
        private readonly EventDispatcherInterface $eventDispatcher
    ) {
    }

    public function subscribeToEvents(): void
    {
        // In-chart card, rendered at the TOP of the demographics page.
        $this->eventDispatcher->addListener(
            RenderEvent::EVENT_SECTION_LIST_RENDER_TOP,
            $this->renderCopilotCard(...)
        );
        // Top-level "Co-Pilot" main-menu item → My Day schedule view.
        $this->eventDispatcher->addListener(
            MenuEvent::MENU_UPDATE,
            $this->addCopilotMenuItem(...)
        );
    }

    /**
     * Configurable sidecar base URL: $GLOBALS override if set, else the default.
     */
    public function sidecarUrl(): string
    {
        $configured = $GLOBALS['copilot_sidecar_url'] ?? null;
        if (is_string($configured) && $configured !== '') {
            return $configured;
        }
        return self::DEFAULT_SIDECAR_URL;
    }

    /**
     * Render the Co-Pilot as a FLOATING OVERLAY helper, scoped to the open
     * patient via $event->getPid(). Closed state is a launcher bubble
     * (bottom-right, live alert badge); clicking it slides open the full panel.
     * The panel posts {copilot:"state", open} and this wrapper resizes the
     * iframe — origin-checked, no PHI in messages. Because it renders only on
     * the patient chart, it appears with a patient and vanishes everywhere else.
     */
    public function renderCopilotCard(RenderEvent $event): void
    {
        // Real OpenEMR ACL enforcement (delegated to gacl): a user who lacks
        // patients|demo gets NO panel and NO token. This is the authoritative
        // authorization decision the sidecar then relies on.
        if (!AclMain::aclCheckCore('patients', 'demo')) {
            return;
        }
        $pid = $event->getPid();
        $sidecar = $this->sidecarUrl();
        $origin = preg_replace('#^(https?://[^/]+).*$#', '$1', $sidecar);

        // Mint two short-lived tokens from the OpenEMR session: a patient-bound
        // one for the "This Patient" view, and a day-scoped one so the "My Day"
        // tab (which spans the provider's whole schedule) works in the overlay.
        // Failure to mint (missing key) fails closed — no panel.
        try {
            $uid = CopilotToken::currentUserId();
            $usr = CopilotToken::currentUsername();
            $key = CopilotToken::signingKey();
            $now = time();
            $token = CopilotToken::mint($uid, $usr, (int)$pid, 'patient', 'patients:demo', $key, CopilotToken::TTL_SECONDS, $now);
            $dayToken = CopilotToken::mint($uid, $usr, 0, 'day', 'patients:demo', $key, CopilotToken::TTL_SECONDS, $now);
        } catch (\Throwable $e) {
            error_log('copilot: token mint failed: ' . $e->getMessage());
            return;
        }
        // Tokens travel in the URL FRAGMENT — never sent to the server, so they
        // stay out of the sidecar's access logs; the panel JS reads location.hash.
        $src = $sidecar . "/?pid=" . urlencode((string)$pid) . "&embed=overlay#t=" . $token . "&d=" . $dayToken;
        ?>
        <script>
        (function () {
            var ORIGIN = <?php echo json_encode($origin); ?>;
            var SRC = <?php echo json_encode($src); ?>;
            var PID = <?php echo json_encode((string)$pid); ?>;
            // Mount the overlay in the TOP browser window so `position:fixed` is
            // relative to the real viewport. OpenEMR renders this event inside a
            // SCROLLING tab frame; a fixed element pinned there lands at the tab
            // frame's content edge (often off-screen → a horizontal scroll to
            // reach the bubble). Same-origin throughout, so top.document is ours;
            // fall back to this document if ever blocked.
            var host, hostWin;
            try { host = window.top.document; hostWin = window.top; host.body.appendChild; }
            catch (e) { host = document; hostWin = window; }

            var f = host.getElementById('copilot-overlay');
            if (f) {
                // Already mounted (persists across tab-content reloads). If the
                // physician opened a DIFFERENT patient, re-point it to that pid.
                if (f.getAttribute('data-pid') !== PID) {
                    f.src = SRC;
                    f.setAttribute('data-pid', PID);
                }
                return;
            }
            f = host.createElement('iframe');
            f.id = 'copilot-overlay';
            f.title = 'Clinical Co-Pilot';
            f.src = SRC;
            f.setAttribute('data-pid', PID);
            f.setAttribute('allowtransparency', 'true');
            // max-width/height keep it inside the viewport so it can never force
            // a page scroll; it opens to a comfortable size, not a squished strip.
            // Anchored bottom-right (grows upward when opened) — now reliable
            // because the iframe lives in the top window, not the scrolling tab.
            f.style.cssText =
                'position:fixed;bottom:18px;right:18px;width:76px;height:76px;border:0;'
                + 'z-index:2147483000;background:transparent;color-scheme:normal;'
                + 'max-width:calc(100vw - 24px);max-height:calc(100vh - 28px);'
                + 'transition:width .2s ease,height .2s ease;';
            host.body.appendChild(f);

            // Normalize an origin for comparison: strip a trailing slash and
            // lower-case, so "https://host" and "https://host/" are treated as
            // equal. Scheme/host drift from a trailing slash must NOT cause us to
            // silently drop a legitimate state message (that was the bug: a
            // rejected message left the iframe frozen at 76px until page reload).
            function normOrigin(o) { return String(o || '').replace(/\/+$/, '').toLowerCase(); }
            var WANT_ORIGIN = normOrigin(ORIGIN);

            hostWin.addEventListener('message', function (e) {
                // Accept the message when EITHER the normalized origin matches OUR
                // sidecar origin, OR the message physically came from our own
                // iframe's contentWindow. The second check is the self-heal: it
                // still proves the sender is our panel (not an arbitrary origin),
                // but survives trailing-slash/scheme drift in e.origin. We never
                // accept messages from unknown windows or origins.
                var fromOurFrame = (e.source && e.source === f.contentWindow);
                var originOk = (normOrigin(e.origin) === WANT_ORIGIN);
                if ((!originOk && !fromOurFrame) || !e.data || e.data.copilot !== 'state') { return; }
                // Idempotently derive the iframe size from e.data.open on EVERY
                // state message, so a re-asserted (retried) message reconciles the
                // parent even if an earlier one was dropped. open -> grown size,
                // anything else -> the 76px launcher bubble.
                if (e.data.open) {
                    f.style.width = Math.min(680, Math.max(380, (hostWin.innerWidth || 1200) - 40)) + 'px';
                    f.style.height = Math.min(860, Math.max(460, (hostWin.innerHeight || 800) - 100)) + 'px';
                } else {
                    f.style.width = '76px';
                    f.style.height = '76px';
                }
            });
        })();
        </script>
        <?php
    }

    /**
     * Add a top-level "Co-Pilot" item to the main menu, opening the My Day page.
     */
    public function addCopilotMenuItem(MenuEvent $event): MenuEvent
    {
        $menu = $event->getMenu();

        // Match OpenEMR's standard top-level item shape so it renders like a
        // native main-menu entry and opens in the main content frame.
        $item = new \stdClass();
        $item->requirement = 0;
        $item->target = 'main';
        $item->menu_id = 'copilot0';
        $item->label = xlt("Co-Pilot");
        $item->on_click = 'top.restoreSession()';
        $item->url = "/interface/modules/custom_modules/oe-module-copilot/public/myday.php";
        $item->pid = 'false';
        $item->children = [];
        $item->acl_req = ["patients", "appt"];
        $item->global_req = [];

        // Insert near the front so it sits alongside the primary clinical menus.
        array_splice($menu, 1, 0, [$item]);

        $event->setMenu($menu);
        return $event;
    }
}
