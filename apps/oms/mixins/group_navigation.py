"""
Mixin for human-like WhatsApp group navigation.
Provides progressive typing with mid-typing result detection.
"""

import asyncio
import random
from typing import TYPE_CHECKING, Tuple, Optional

from apps.oms.shared.logger import get_logger

if TYPE_CHECKING:
    from apps.oms.infrastructure.browser.session_manager import SessionManager


class GroupNavigationMixin:
    """
    Mixin providing human-like WhatsApp group navigation.
    
    Features:
    - Checks if already in the target group
    - Waits intelligently for manual navigation before automating
    - Progressive typing with natural rhythm
    - Mid-typing result detection
    - Human-like hesitation and pauses
    - Automatic click when group appears
    - Fallback to full name if not found early
    - Idempotent - safe to call multiple times
    """
    
    async def open_target_group(
        self: 'SessionManager', 
        group_name: str,
        manual_wait_seconds: int = 10,
        check_interval: float = 0.5
    ) -> bool:
        '''
        Navigate to the configured WhatsApp group.
        Safe to call multiple times - will check if already in the group.

        This intelligently waits for manual navigation before automating:
        1. First checks if already in the target group
        2. If not, waits for `manual_wait_seconds` for user to manually navigate
        3. Logs waiting status and remaining time
        4. If user navigates manually during wait, returns success
        5. If wait expires, proceeds with automated navigation

        Strategy:
          0. Check if already in target group (return early if yes)
          1. Wait for manual navigation with progress logging
          2. If not navigated manually, perform automated navigation:
             a. Click the WhatsApp search input
             b. Type group name with human rhythm
             c. Check for results after each significant chunk
             d. Click the group as soon as it appears
             e. If not found mid-typing, complete full name
          3. Verify the group chat is open

        Args:
            group_name: Name of the group to open
            manual_wait_seconds: Seconds to wait for manual navigation before automating
            check_interval: How often to check if manually navigated

        Returns:
            True if group was found and opened successfully.
            False if group could not be found.
        '''
        log = get_logger("oms.runner")
        page = self._page
        
        SEARCH_SEL = 'input[aria-label="Search or start a new chat"]'

        log.info(f"Opening target group: {group_name!r}")

        # ── Check if already in the target group ──────────────────
        if await self._is_in_target_group(group_name):
            log.info(f"✅ Already in target group: {group_name!r}")
            return True

        # ── Wait for manual navigation ────────────────────────────
        log.info(f"⏳ Not in target group. Waiting {manual_wait_seconds}s for manual navigation...")
        log.info(f"💡 You can manually navigate to '{group_name}' during this time")
        
        manual_navigation_detected = False
        elapsed = 0
        
        while elapsed < manual_wait_seconds:
            # Check if user manually navigated to the group
            if await self._is_in_target_group(group_name):
                manual_navigation_detected = True
                log.info(f"✅ Manual navigation detected! User opened: {group_name!r}")
                return True
            
            # Log progress every 2 seconds
            if int(elapsed) % 2 == 0 and elapsed > 0:
                remaining = manual_wait_seconds - elapsed
                log.info(f"⏳ Still waiting... {remaining:.1f}s remaining for manual navigation")
            
            await asyncio.sleep(check_interval)
            elapsed += check_interval
        
        # ── Manual wait expired, proceed with automated flow ──────
        log.info(f"⏰ Manual wait expired. Proceeding with automated navigation to: {group_name!r}")

        # ── Human-like typing helpers ──────────────────────────────
        def get_typing_delay(char: str, prev_char: str = None) -> float:
            """Simulate natural typing rhythm with contextual pauses."""
            base_delay = random.uniform(80, 180)  # ms
            
            # Pause longer after certain characters (like finishing a word)
            if char in ' .,!?':
                return base_delay * random.uniform(1.8, 3.5)
            
            # Pause slightly before/after certain letters (common in human typing)
            if char in 'aeiou':
                return base_delay * random.uniform(0.8, 1.2)
            
            if char in 'bcdfghjklmnpqrstvwxyz':
                return base_delay * random.uniform(0.9, 1.3)
            
            # Pause longer when starting a new "thought" (every few words)
            if prev_char and prev_char in ' .,!?':
                return base_delay * random.uniform(1.5, 2.8)
            
            return base_delay

        def should_check_results(current_text: str) -> bool:
            """Determine when to check for results based on typing progress."""
            # Check after completing a word or at specific intervals
            if len(current_text) >= 2 and current_text[-1] in ' .,!?':
                return True
            
            # Check after 3, 6, 9, 12 characters (natural checkpoints)
            checkpoints = [3, 6, 9, 12, 15]
            if len(current_text) in checkpoints:
                return True
            
            # Random check (10% chance at any point - human curiosity)
            if random.random() < 0.10:
                return True
            
            return False

        async def check_and_click_result(typed_so_far: str) -> Tuple[bool, str]:
            """
            Check if the target group appears in search results.
            Returns: (clicked, matched_text)
            """
            if len(typed_so_far) < 2:
                return False, ""
            
            # Various selectors that might match the group at any stage
            result_sels = [
                f'span[title^="{typed_so_far}"]',  # Starts with typed text
                f'span[title*="{typed_so_far}"]',   # Contains typed text
                f'div[role="listitem"] span[title*="{typed_so_far}"]',
                f'div[data-testid="cell-frame-container"] span[title*="{typed_so_far}"]',
            ]
            
            for sel in result_sels:
                try:
                    elements = await page.query_selector_all(sel)
                    for el in elements:
                        if await el.is_visible():
                            # Get the actual text to verify it's our group
                            title = await el.get_attribute("title")
                            if title and group_name in title:
                                # Human-like hesitation before clicking
                                await asyncio.sleep(random.uniform(0.2, 0.6))
                                await el.click()
                                log.info(f"✅ Clicked group mid-typing after '{typed_so_far}'")
                                return True, title
                except Exception:
                    continue
            
            return False, ""

        # ── Click search bar with natural movement ──────────────────
        try:
            log.debug("Looking for search bar...")
            search_el = await page.wait_for_selector(
                SEARCH_SEL,
                timeout=8_000
            )
            
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await search_el.click()
            await asyncio.sleep(random.uniform(0.1, 0.3))
            log.debug("Search bar clicked")
        except Exception as e:
            log.error(f"Could not find search bar: {e}")
            return False

        # ── Clear existing text with human-like keypresses ──────────
        log.debug("Clearing search field...")
        await page.keyboard.down("Control")
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.keyboard.press("a")
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await page.keyboard.up("Control")
        
        await asyncio.sleep(random.uniform(0.1, 0.25))
        await page.keyboard.press("Delete")
        await asyncio.sleep(random.uniform(0.3, 0.7))

        # ── Progressive typing with mid-typing result checking ──────
        log.debug(f"Progressive typing: {group_name!r}")
        
        typed_text = ""
        clicked_early = False
        
        # Sometimes "hesitate" before starting to type
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(0.3, 0.8))
        
        # Type character by character, checking for results progressively
        for i, char in enumerate(group_name):
            # Get natural typing delay
            prev_char = group_name[i-1] if i > 0 else None
            delay = get_typing_delay(char, prev_char)
            
            # Type the character
            await page.keyboard.type(char, delay=delay)
            typed_text += char
            
            # Small chance of a "thinking" pause mid-typing
            if random.random() < 0.06:
                await asyncio.sleep(random.uniform(0.05, 0.15))
            
            # Check if we should look for results
            if should_check_results(typed_text):
                # Pause briefly like a human checking the screen
                await asyncio.sleep(random.uniform(0.1, 0.3))
                
                # Check for the group in results
                clicked, matched = await check_and_click_result(typed_text)
                if clicked:
                    clicked_early = True
                    break
                
                # If not found, continue typing with a slight "disappointed" pause
                await asyncio.sleep(random.uniform(0.05, 0.15))
        
        # ── If not clicked mid-typing, try the full name ──────────
        if not clicked_early:
            log.debug("Group not found mid-typing, completing full name")
            
            # Finish typing the full name if we stopped early
            remaining = group_name[len(typed_text):]
            for char in remaining:
                prev_char = group_name[i-1] if len(typed_text) > 0 else None
                delay = get_typing_delay(char, prev_char)
                await page.keyboard.type(char, delay=delay)
                typed_text += char
            
            # Wait for results with human-like impatience
            await asyncio.sleep(random.uniform(0.6, 1.2))
            
            # Try clicking with full name
            clicked, _ = await check_and_click_result(typed_text)
            if not clicked:
                # Last resort: try clicking the first result
                log.debug("Attempting fallback: clicking first result")
                try:
                    first_result = await page.query_selector('div[role="listitem"]:first-child')
                    if first_result:
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        await first_result.click()
                        clicked = True
                        log.debug("Clicked first search result as fallback")
                except Exception:
                    pass
                
                if not clicked:
                    log.warning(f"❌ Could not find group: {group_name!r}")
                    return False
        
        # ── Verify group chat opened ─────────────────────────────
        await asyncio.sleep(random.uniform(0.8, 1.5))
        
        # Check if we successfully opened the group
        if await self._is_in_target_group(group_name):
            log.info(f"✅ Group chat opened successfully: {group_name!r}")
            return True

        log.warning(f"❌ Could not confirm group opened: {group_name!r}")
        return False

    async def _is_in_target_group(self: 'SessionManager', group_name: str) -> bool:
        """
        Check if currently in the target group chat.
        
        Returns:
            True if in the target group, False otherwise.
        """
        page = self._page
        
        try:
            # Check multiple selectors to determine if we're in the right chat
            header_sels = [
                f'header span[title="{group_name}"]',
                f'span[title="{group_name}"]',
                'div[data-testid="conversation-header"]',
                'header',
            ]
            
            for sel in header_sels:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    # Get the text to verify it matches the group name
                    if f'[title="{group_name}"]' in sel:
                        return True
                    elif sel in ['div[data-testid="conversation-header"]', 'header']:
                        # For generic selectors, check the text content
                        text = await el.text_content()
                        if text and group_name in text:
                            return True
            
            return False
            
        except Exception:
            return False