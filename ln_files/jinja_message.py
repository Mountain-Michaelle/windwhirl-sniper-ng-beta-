# ==============================================================
# FINAL TEMPLATES — Warm Nigerian voice, skillfully brief
# ==============================================================
#   FILE A → apps/templates/message_a.j2
#   FILE B → apps/templates/message_b.j2
#
# WHAT CHANGED FROM VERSION 1 (too long):
#   Removed the paragraph explaining what they bought in detail
#   Removed the formal sign-off block
#   Kept everything that had warmth and personality
#
# WHAT CHANGED FROM VERSION 2 (too robotic):
#   Removed the bullet-point feel
#   Restored natural Nigerian sentence flow
#   Discount is still visible early but feels earned not forced
#   "How is the glow?" kept — it works
#
# TARGET LENGTH: What fits on one phone screen without scrolling
# ==============================================================


# ==============================================================
# ================================================================
#  TEMPLATE A
#  PATH: apps/templates/message_a.j2
#  STYLE: Warm check-in — feels like a message from someone
#         who genuinely remembers you
# ================================================================
#
# LINE BY LINE BREAKDOWN:
#
#  "Good day {{ first_name }}!"
#   → "Good day" is natural Nigerian English. More flexible than
#     "Good afternoon" since the scheduler sends across sessions.
#     Warm, human, immediately feels personal.
#
#  "It's Michael from Nabeau Store 😊"
#   → Identity on line 1. They know who this is before reading
#     anything else. The emoji softens it — not corporate.
#
#  "How has your Sadoer Collagen Set been treating you?"
#   → One question. Specific to their product. "Treating you"
#     is Nigerian English — people say this naturally.
#     Makes them think about their experience before the ask.
#
#  "🎁 Share your experience..."
#   → Emoji as visual anchor for skimmers. Discount visible
#     immediately. "I'll sort you out" is Nigerian English for
#     "I will take care of you" — warm, informal, trustworthy.
#
#  "Even a voice note is fine 🎙️"
#   → Removes effort barrier. Last line stays short.
# ================================================================
# ==============================================================

# ── COPY INTO: apps/templates/message_a.j2 ────────────────────

"""
Good day {{ first_name }}! It's Michael from Nabeau Store 😊

How has your Sadoer Collagen Set been treating you? We'd love to hear your honest experience — good or bad, it all helps us. ✨

🎁 Share your thoughts and I'll sort you out with {{ discount_offer }} on your next order.

Even a voice note is fine 🎙️
{% if review_link %}
{{ review_link }}
{% endif %}
"""

# ── END OF TEMPLATE A ─────────────────────────────────────────


# ==============================================================
# ================================================================
#  TEMPLATE B
#  PATH: apps/templates/message_b.j2
#  STYLE: "Real talk" — honest, slightly playful, peer energy
# ================================================================
#
# LINE BY LINE BREAKDOWN:
#
#  "{{ first_name }}! 👋"
#   → Name first, emoji second. Feels like someone calling
#     your name across the room. Immediate attention grab.
#
#  "It's Michael from Nabeau Store..."
#   → Identity + product reference in one line.
#     "some time ago" is natural, not clinical.
#
#  "Can I ask — how did it go for you?"
#   → "Can I ask" is how Nigerians soften a direct question.
#     It signals respect before asking. Very natural phrasing.
#     Short. Easy to answer.
#
#  "We're not looking for perfect..."
#   → Removes all performance pressure. Customers who had
#     mixed results will now respond instead of staying silent.
#     "your real experience" signals authenticity.
#
#  "🎁 Your honest words = {{ discount_offer }}"
#   → Discount visible with emoji anchor. "No long thing"
#     is pure Nigerian English — signals you get their culture
#     and won't waste their time.
# ================================================================
# ==============================================================

# ── COPY INTO: apps/templates/message_b.j2 ────────────────────

"""
{{ first_name }}! 👋 It's Michael from Nabeau Store — you got our Sadoer Collagen Set from us some time ago.

Can I ask — how did it go for you? Did you notice any difference? ✨

We're not looking for perfect, just your real experience. It genuinely helps other people decide.

🎁 Your honest words = {{ discount_offer }} on your next order. No long thing, just reply here or send a quick voice note 🎙️
{% if review_link %}
{{ review_link }}
{% endif %}
"""

# ── END OF TEMPLATE B ─────────────────────────────────────────


# ==============================================================
# SIDE BY SIDE — YOUR ORIGINAL VS THESE FINALS
# ==============================================================
#
# YOUR ORIGINAL:
# ──────────────
# Good Afternoon Mrs. Stephen,
# It's Michael from Nabeau Store.
#
# You purchased our Sadoer Collagen Set (Collagen Face Serum
# and Face Cream) some while ago.
#
# We need your honest testimonial about our product.
# This would attract some discount on your next purchase.
#
# Thank you.
# ──────────────
# Problems: "We need" = pressure. "Would attract" = weak.
# "Mrs. Stephen" = distant. Discount buried. 9 lines.
#
#
# TEMPLATE A (rendered for Blessing):
# ──────────────
# Good day Blessing! It's Michael from Nabeau Store 😊
#
# How has your Sadoer Collagen Set been treating you?
# We'd love to hear your honest experience — good or bad,
# it all helps us. ✨
#
# 🎁 Share your thoughts and I'll sort you out with
# 10% off your next order.
#
# Even a voice note is fine 🎙️
# ──────────────
# 6 lines. Warm. Discount visible. One ask. Voice note offered.
#
#
# TEMPLATE B (rendered for Titilayo):
# ──────────────
# Titilayo! 👋 It's Michael from Nabeau Store — you got
# our Sadoer Collagen Set from us some time ago.
#
# Can I ask — how did it go for you?
# Did you notice any difference? ✨
#
# We're not looking for perfect, just your real experience.
# It genuinely helps other people decide.
#
# 🎁 Your honest words = 10% off your next order.
# No long thing, just reply here or send a quick voice note 🎙️
# ──────────────
# 7 lines. Honest energy. Pressure-free. Nigerian phrasing.
# Discount visible. Lowest possible effort ask.
#
# ==============================================================
# KEY NIGERIAN ENGLISH PHRASES USED AND WHY
# ==============================================================
#
# "How has it been treating you?"
#   → Natural Nigerian way of asking "how is it working"
#     More personal than "what are your results"
#
# "I'll sort you out"
#   → Nigerian English for "I will take care of you / give you"
#     Warm and informal. Sounds like a person, not a brand.
#
# "Can I ask"
#   → How Nigerians soften a direct question politely
#     Shows respect before making a request
#
# "No long thing"
#   → Ubiquitous Nigerian WhatsApp phrase meaning
#     "I won't take much of your time / keep it simple"
#     Immediately signals cultural familiarity
#
# "Good day"
#   → Works morning to evening. More flexible than
#     "Good afternoon" for an automated system.
#     Natural Nigerian English greeting.
#
# ==============================================================
# AFTER SAVING BOTH FILES
# ==============================================================
# Test with:
#   python main.py --dry-run
#
# You will see both templates rendered with real customer
# names from your Excel file. Read them out loud — if they
# sound like something you would actually type to a customer
# on WhatsApp, they are ready.
# ==============================================================