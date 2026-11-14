<?php

/**
 * Short-lived HMAC session token minted by OpenEMR for the Clinical Co-Pilot.
 *
 * Replaces the Week-1 reusable static bearer (which was exposed in the browser).
 * A token is minted ONLY after a real OpenEMR ACL check (AclMain::aclCheckCore)
 * and is bound to the logged-in user + the selected patient, with a short TTL.
 * The Python sidecar verifies it (see copilot/AUTH_TOKEN_SPEC.md — the shared
 * contract). The signature covers the LITERAL base64url payload segment, so the
 * sidecar never re-serializes the JSON and PHP<->Python encoding differences are
 * irrelevant.
 *
 * @package OpenEMR\Modules\Copilot
 * @license https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\Copilot;

class CopilotToken
{
    /** Default token lifetime (seconds). Kept short — this is a session token. */
    const TTL_SECONDS = 900;

    /**
     * Mint `payload_b64url.sig_b64url`.
     *
     * @param int    $sub   OpenEMR users.id (from $_SESSION['authUserID'])
     * @param string $usr   OpenEMR username (audit only)
     * @param int    $pid   selected patient (0 for a day/ops token)
     * @param string $scope "patient" | "day"
     * @param string $acl   the ACL verified at mint, e.g. "patients:demo"
     * @param string $key   the shared HMAC secret (UTF-8 bytes used as-is)
     * @param int    $ttl   lifetime in seconds
     * @param int    $now   issued-at (unix seconds)
     */
    public static function mint(
        int $sub,
        string $usr,
        int $pid,
        string $scope,
        string $acl,
        string $key,
        int $ttl,
        int $now
    ): string {
        // Key order is irrelevant: the sidecar verifies over the literal segment.
        $payload = [
            'v' => 1,
            'sub' => $sub,
            'usr' => $usr,
            'pid' => $pid,
            'scope' => $scope,
            'acl' => $acl,
            'iat' => $now,
            'exp' => $now + $ttl,
        ];
        $json = json_encode($payload, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
        $payloadB64 = self::b64url($json);
        $sig = self::b64url(hash_hmac('sha256', $payloadB64, $key, true));
        return $payloadB64 . '.' . $sig;
    }

    /** base64url, no padding — matches the sidecar's decoder. */
    private static function b64url(string $raw): string
    {
        return rtrim(strtr(base64_encode($raw), '+/', '-_'), '=');
    }

    /**
     * The shared HMAC secret, read from the openemr DB (seeded, stable). The
     * sidecar reads the same row via its read-only connection.
     *
     * @throws \RuntimeException if the key row is absent (fail closed — no mint).
     */
    public static function signingKey(): string
    {
        $row = sqlQuery("SELECT secret FROM copilot_signing_key WHERE id = 1");
        if (empty($row['secret'])) {
            throw new \RuntimeException('copilot signing key missing');
        }
        return (string)$row['secret'];
    }

    /**
     * The logged-in user's numeric OpenEMR `users.id` — the token's `sub`, which
     * the sidecar checks against `patient_data.providerID`.
     *
     * `$_SESSION['authUserID']` is the canonical source, but it is not reliably
     * populated in every render context (the demographics render event can fire
     * with only `$_SESSION['authUser']` — the username — set, which is why an
     * earlier build minted sub=0 and the sidecar denied every patient as
     * "unknown_user"). Fall back to resolving the id from the username.
     */
    /**
     * OpenEMR's session is a Symfony session accessed via the SessionWrapper —
     * the raw `$_SESSION` superglobal does NOT carry `authUserID` / `authUser`
     * (Symfony namespaces them), which is why an earlier build read NULL and
     * minted sub=0. Read the active session the same way `globals.php` does.
     */
    private static function sessionGet(string $key): ?string
    {
        try {
            $session = \OpenEMR\Common\Session\SessionWrapperFactory::getInstance()->getActiveSession();
            $v = $session->get($key);
            return ($v === null || $v === '') ? null : (string)$v;
        } catch (\Throwable $e) {
            $v = $_SESSION[$key] ?? null;  // fallback for older cores
            return ($v === null || $v === '') ? null : (string)$v;
        }
    }

    public static function currentUsername(): string
    {
        return self::sessionGet('authUser') ?? '';
    }

    public static function currentUserId(): int
    {
        $uid = (int)(self::sessionGet('authUserID') ?? 0);
        if ($uid <= 0) {
            $uname = self::currentUsername();
            if ($uname !== '') {
                $row = sqlQuery("SELECT id FROM users WHERE username = ?", [$uname]);
                $uid = (int)($row['id'] ?? 0);
            }
        }
        return $uid;
    }
}
