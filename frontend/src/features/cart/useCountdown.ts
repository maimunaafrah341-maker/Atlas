import { useEffect, useEffectEvent, useState } from "react";

/**
 * Milliseconds left until `expiresAt` (an ISO time from the server), ticking each second, or null
 * when there is nothing to count down. `onExpire` runs once when it reaches zero.
 *
 * The countdown uses this device's clock, so it is approximate; the server alone decides when a
 * reservation has expired (lazily, on the next read), which is why expiry triggers a reload.
 */
export function useCountdown(expiresAt: string | null, onExpire: () => void): number | null {
  const [now, setNow] = useState(() => Date.now());
  const expire = useEffectEvent(onExpire);
  const deadline = expiresAt === null ? null : Date.parse(expiresAt);

  useEffect(() => {
    if (deadline === null) return;
    let fired = false;
    const tick = () => {
      const current = Date.now();
      setNow(current);
      if (!fired && current >= deadline) {
        fired = true;
        expire();
      }
    };
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [deadline]);

  return deadline === null ? null : Math.max(0, deadline - now);
}

/** "14:05" from milliseconds (rounded up, so it never shows 0:00 while time remains). */
export function formatCountdown(ms: number): string {
  const total = Math.ceil(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
