 ==============================================================
# EXACT FIXES — based on your actual attached source files
# ==============================================================
#
# TWO CHANGES ONLY:
#
# FIX 1: profile.py — has_cookies detection wrong path
#   Cookies are at Default/Network/Cookies (confirmed by diagnostic)
#   not Default/Cookies. Fix the inspect() method.
#
# FIX 2: dom_observer.py — _inject_observer() needs retry loop
#   Current code: returns False immediately on container_not_found.
#   Fix: retry up to 10 times with 2s between attempts.
#
# Both files attached by you, so edits reference exact line content.
# ==============================================================


# ==============================================================
# FIX 1
# FILE: apps/oms/infrastructure/browser/profile.py
# ==============================================================
# In the inspect() method, FIND this block:
#
#         # Check for Chromium's Cookies file as a session indicator
#         cookies_path = self._path / "Default" / "Cookies"
#         has_cookies  = cookies_path.exists() and cookies_path.stat().st_size > 1024
#
# REPLACE WITH:
#
        # # Chromium stores cookies at Default/Cookies (older)
        # # or Default/Network/Cookies (newer, Windows confirmed).
        # # Check both — whichever exists and has data counts.
        # cookies_old = self._path / "Default" / "Cookies"
        # cookies_new = self._path / "Default" / "Network" / "Cookies"
        # has_cookies = (
        #     (cookies_old.exists() and cookies_old.stat().st_size > 1024)
        #     or
        #     (cookies_new.exists() and cookies_new.stat().st_size > 1024)
        # )

# That's the only change in profile.py.
# After this fix: "cookies=True" on startup, no more QR prompts.
# ==============================================================


# ==============================================================
# FIX 2
# FILE: apps/oms/infrastructure/browser/dom_observer.py
# ==============================================================
# FIND the entire _inject_observer() method (lines shown from
# your attached file) and REPLACE it completely:
#
# FIND (the whole method):
#
#     async def _inject_observer(self) -> bool:
#         '''
#         Inject the JavaScript MutationObserver into the page.
#         Returns True if injection succeeded.
#         '''
#         try:
#             result = await self._page.evaluate(_MUTATION_OBSERVER_JS)
#             status = result.get("status", "unknown")
#
#             if status == "active":
#                 log.info(
#                     f"MutationObserver injected. "
#                     f"Container: {result.get('containerTag', '?')}"
#                 )
#                 return True
#
#             elif status == "already_active":
#                 log.debug("MutationObserver already active — skipping injection.")
#                 return True
#
#             elif status == "container_not_found":
#                 log.warning(
#                     "WhatsApp chat container not found.\n"
#                     "Is the group chat open in the browser?"
#                 )
#                 return False
#
#             else:
#                 log.warning(f"Unexpected injection status: {status!r}")
#                 return False
#
#         except Exception as e:
#             log.error(f"Observer injection error: {e}", exc_info=True)
#             return False
#
# REPLACE WITH:

"""
    MAX_INJECT_ATTEMPTS = 10
    INJECT_RETRY_DELAY  = 2.0

    async def _inject_observer(self) -> bool:
        '''
        Inject the JavaScript MutationObserver into the page.
        Retries up to MAX_INJECT_ATTEMPTS times with INJECT_RETRY_DELAY
        between attempts — WhatsApp Web renders the chat container
        asynchronously after group navigation, so the first attempt
        often fires before the container exists in the DOM.
        Returns True if injection succeeded.
        '''
        for attempt in range(1, self.MAX_INJECT_ATTEMPTS + 1):
            try:
                result = await self._page.evaluate(_MUTATION_OBSERVER_JS)
                status = result.get("status", "unknown")

                if status == "active":
                    log.info(
                        f"MutationObserver injected "
                        f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS}). "
                        f"Container: {result.get('containerTag', '?')}"
                    )
                    return True

                elif status == "already_active":
                    log.debug("MutationObserver already active — skipping injection.")
                    return True

                elif status == "container_not_found":
                    log.debug(
                        f"Chat container not ready "
                        f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS}) "
                        f"— retrying in {self.INJECT_RETRY_DELAY}s..."
                    )
                    if attempt < self.MAX_INJECT_ATTEMPTS:
                        await asyncio.sleep(self.INJECT_RETRY_DELAY)
                    continue

                else:
                    log.warning(
                        f"Unexpected injection status: {status!r} "
                        f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS})"
                    )
                    if attempt < self.MAX_INJECT_ATTEMPTS:
                        await asyncio.sleep(self.INJECT_RETRY_DELAY)
                    continue

            except Exception as e:
                log.debug(
                    f"Injection error "
                    f"(attempt {attempt}/{self.MAX_INJECT_ATTEMPTS}): {e}"
                )
                if attempt < self.MAX_INJECT_ATTEMPTS:
                    await asyncio.sleep(self.INJECT_RETRY_DELAY)

        log.error(
            f"Failed to inject MutationObserver after "
            f"{self.MAX_INJECT_ATTEMPTS} attempts "
            f"({self.MAX_INJECT_ATTEMPTS * self.INJECT_RETRY_DELAY:.0f}s total).\n"
            "Check that the group chat is visible and fully loaded in the browser."
        )
        return False
"""

# ==============================================================
# FIX 2B — also expand the JS findContainer() selector list
# ==============================================================
# Inside _MUTATION_OBSERVER_JS string, FIND:
#
#     const findContainer = () => {
#         return (
#             document.querySelector('#main div[role="application"]') ||
#             document.querySelector('#main .copyable-area') ||
#             document.querySelector('#main')
#         );
#     };
#
# REPLACE WITH:
#
    # const findContainer = () => {
    #     return (
    #         document.querySelector('#main div[role="application"]') ||
    #         document.querySelector('div[data-testid="conversation-panel-messages"]') ||
    #         document.querySelector('div[data-testid="msg-container"]') ||
    #         document.querySelector('#main .copyable-area') ||
    #         document.querySelector('#main div[tabindex="-1"]') ||
    #         document.querySelector('div[data-tab="8"]') ||
    #         document.querySelector('#main') ||
    #         document.querySelector('div[data-testid="conversation-panel"]')
    #     );
    # };

# ==============================================================


# ==============================================================
# EXPECTED OUTPUT AFTER BOTH FIXES
# ==============================================================
#
# Run: python oms_runner.py
#
# You should now see:
#
#   [INFO] Profile: ...oms_session
#          exists=True, size=13.00MB, cookies=True       ← was False
#   [INFO] Saved session detected — will attempt silent restore.
#   [INFO] ✅ WhatsApp session restored. OMS is ready.   ← no more QR
#   ...
#   [INFO] ✅ Already in target group: 'General Order Group'
#   [DEBUG] Chat container not ready (attempt 1/10) — retrying in 2.0s...
#   [DEBUG] Chat container not ready (attempt 2/10) — retrying in 2.0s...
#   [INFO] MutationObserver injected (attempt 3/10). Container: DIV
#   [INFO] DOM Observer active. Watching: 'General Order Group'
#   [INFO] Poll interval: 2.0s | Detection: instant (MutationObserver)
#
# Then when a message arrives in the group:
#   [INFO] 📨 New message #1: INCOMING | 'Sender Name' | 'message preview'
#
# ==============================================================
# ALSO: fix staff_number warning while you're in the files
# ==============================================================
# In your .env file:
#   OMS_WHATSAPP_STAFF_NUMBER=09029971712
# Change to:
#   OMS_WHATSAPP_STAFF_NUMBER=2349029971712
#
# Your number is valid Nigerian — just needs 234 prefix not 0 prefix.
# ==============================================================