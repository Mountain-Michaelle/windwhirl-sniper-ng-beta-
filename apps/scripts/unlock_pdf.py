#!/usr/bin/env python3
"""
ULTIMATE PDF CRACKER - 100% FOCUSED ON YOUR DATA
Account: 2294641279 | DOB: 23-07-1998 | Phone: 09156084052
This WILL crack your PDF if the password is based on your info
"""

import sys
import os
import time
import itertools
from datetime import datetime
from PyPDF2 import PdfReader, PdfWriter

# ============================================================
# YOUR PERSONAL DATA - Customize these if needed
# ============================================================
ACCOUNT = "2294641279"
DOB = "23071998"  # DDMMYYYY format
PHONE = "09156084052"

# Parse DOB
DAY = DOB[:2]
MONTH = DOB[2:4]
YEAR = DOB[4:8]
YEAR_SHORT = YEAR[2:]

# ============================================================
# PASSWORD GENERATOR - Generates ALL smart combinations
# ============================================================
def generate_all_passwords():
    """Generate every possible password based on your data"""
    
    passwords = set()
    
    # Account parts
    acc = ACCOUNT
    acc_parts = [
        acc,                    # 2294641279
        acc[:4],                # 2294
        acc[4:8],               # 6412
        acc[8:],                # 79
        acc[:3],                # 229
        acc[3:7],               # 4641
        acc[7:],                # 279
        acc[:2],                # 22
        acc[-4:],               # 1279
        acc[::-1],              # 9721464922
        acc[:8],                # 22946412
        acc[2:],                # 94641279
        acc[3:],                # 4641279
        acc[-6:],               # 641279
        acc[:6],                # 229464
    ]
    
    # DOB parts
    dob_parts = [
        f"{DAY}{MONTH}{YEAR}",      # 23071998
        f"{MONTH}{DAY}{YEAR}",      # 07231998
        f"{YEAR}{MONTH}{DAY}",      # 19980723
        f"{DAY}{MONTH}{YEAR_SHORT}",# 230798
        f"{MONTH}{DAY}{YEAR_SHORT}",# 072398
        f"{DAY}{MONTH}",            # 2307
        f"{MONTH}{DAY}",            # 0723
        YEAR,                       # 1998
        YEAR_SHORT,                 # 98
        DAY,                        # 23
        MONTH,                      # 07
        f"{DAY}-{MONTH}-{YEAR}",    # 23-07-1998
        f"{MONTH}-{DAY}-{YEAR}",    # 07-23-1998
        f"{DAY}/{MONTH}/{YEAR}",    # 23/07/1998
        f"{MONTH}/{DAY}/{YEAR}",    # 07/23/1998
        f"{DAY}.{MONTH}.{YEAR}",    # 23.07.1998
        f"{MONTH}.{DAY}.{YEAR}",    # 07.23.1998
    ]
    
    # Phone parts
    phone_clean = ''.join(filter(str.isdigit, PHONE))
    phone_parts = [
        phone_clean,                # 09156084052
        phone_clean[2:],            # 156084052
        phone_clean[-10:],          # 9156084052
        phone_clean[::-1],          # 25048065190
        phone_clean[-10:][::-1],    # 2504806519
        phone_clean[:3],            # 091
        phone_clean[2:5],           # 156
        phone_clean[5:8],           # 084
        phone_clean[8:],            # 052
        phone_clean[2:][:5],        # 15608
        phone_clean[-5:],           # 84052
        phone_clean[-6:],           # 084052
        phone_clean[:5],            # 09156
    ]
    
    # Special characters
    specials = ['', '!', '@', '#', '$', '%', '^', '&', '*', '?', '~', '123', '2024', '2025', '2026']
    separators = ['', '-', '_', '.', ' ', '/', '|', ':']
    
    # Bank words
    bank_words = ['BANK', 'ACCOUNT', 'ACC', 'CUST', 'CUSTOMER', 
                  'SBI', 'HDFC', 'ICICI', 'AXIS', 'YES', 'PNB', 'KOTAK', 'IDFC']
    
    print("\n🔐 Generating password combinations...")
    print("=" * 60)
    
    # ============================================================
    # LEVEL 1: Single parts
    # ============================================================
    print("[1/8] Adding basic values...")
    for p in acc_parts:
        passwords.add(p)
    for d in dob_parts:
        passwords.add(d)
    for p in phone_parts:
        passwords.add(p)
    
    # ============================================================
    # LEVEL 2: Account + DOB
    # ============================================================
    print("[2/8] Adding Account + DOB...")
    for acc_part in acc_parts[:6]:
        for dob_part in dob_parts[:6]:
            passwords.add(f"{acc_part}{dob_part}")
            passwords.add(f"{dob_part}{acc_part}")
            passwords.add(f"{acc_part}{dob_part}{acc_part[:4]}")
            passwords.add(f"{dob_part}{acc_part}{dob_part[:4]}")
    
    # ============================================================
    # LEVEL 3: Account + Phone
    # ============================================================
    print("[3/8] Adding Account + Phone...")
    for acc_part in acc_parts[:5]:
        for phone_part in phone_parts[:4]:
            passwords.add(f"{acc_part}{phone_part}")
            passwords.add(f"{phone_part}{acc_part}")
            passwords.add(f"{acc_part[:4]}{phone_part}{acc_part[-4:]}")
    
    # ============================================================
    # LEVEL 4: DOB + Phone
    # ============================================================
    print("[4/8] Adding DOB + Phone...")
    for dob_part in dob_parts[:5]:
        for phone_part in phone_parts[:4]:
            passwords.add(f"{dob_part}{phone_part}")
            passwords.add(f"{phone_part}{dob_part}")
    
    # ============================================================
    # LEVEL 5: Account + DOB + Phone (ALL THREE)
    # ============================================================
    print("[5/8] Adding triple combinations...")
    for acc_part in acc_parts[:4]:
        for dob_part in dob_parts[:4]:
            for phone_part in phone_parts[:3]:
                passwords.add(f"{acc_part}{dob_part}{phone_part}")
                passwords.add(f"{phone_part}{acc_part}{dob_part}")
                passwords.add(f"{dob_part}{acc_part}{phone_part}")
                passwords.add(f"{acc_part}{phone_part}{dob_part}")
    
    # ============================================================
    # LEVEL 6: With separators
    # ============================================================
    print("[6/8] Adding separators...")
    for sep in separators:
        if sep:  # Skip empty (already have those)
            # Account with separators
            if len(acc) >= 10:
                passwords.add(f"{acc[:4]}{sep}{acc[4:8]}{sep}{acc[8:]}")
                passwords.add(f"{acc[:3]}{sep}{acc[3:7]}{sep}{acc[7:]}")
            
            # DOB with separators
            passwords.add(f"{DAY}{sep}{MONTH}{sep}{YEAR}")
            passwords.add(f"{MONTH}{sep}{DAY}{sep}{YEAR}")
            
            # Phone with separators
            p = phone_parts[2] if len(phone_parts) > 2 else phone_clean[-10:]
            if len(p) >= 10:
                passwords.add(f"{p[:3]}{sep}{p[3:6]}{sep}{p[6:]}")
    
    # ============================================================
    # LEVEL 7: With special characters
    # ============================================================
    print("[7/8] Adding special characters...")
    for spec in specials:
        if spec:
            passwords.add(f"{acc}{spec}")
            passwords.add(f"{spec}{acc}")
            passwords.add(f"{acc[-4:]}{spec}")
            passwords.add(f"{dob_parts[0]}{spec}")
            passwords.add(f"{spec}{dob_parts[0]}")
            passwords.add(f"{phone_parts[2]}{spec}")
    
    # ============================================================
    # LEVEL 8: Bank prefixes/suffixes
    # ============================================================
    print("[8/8] Adding bank patterns...")
    for word in bank_words:
        passwords.add(f"{word}{acc}")
        passwords.add(f"{acc}{word}")
        passwords.add(f"{word}{acc[-4:]}")
        passwords.add(f"{word}{dob_parts[0]}")
        passwords.add(f"{word}{phone_parts[2]}")
        passwords.add(f"{word}{acc}{word}")
        passwords.add(f"{word.lower()}{acc}")
        passwords.add(f"{acc}{word.lower()}")
    
    # ============================================================
    # FINAL CLEANUP
    # ============================================================
    # Remove empty and very long passwords
    passwords = {p for p in passwords if p and len(p) <= 35}
    
    # Convert to list and sort by length (shorter first = faster)
    password_list = sorted(list(passwords), key=len)
    
    print(f"\n✅ Generated {len(password_list):,} unique passwords")
    print(f"   Shortest: {len(password_list[0])} chars - {password_list[0]}")
    print(f"   Longest:  {len(password_list[-1])} chars - {password_list[-1][:30]}...")
    
    # Save to file
    with open('passwords_custom.txt', 'w') as f:
        for pwd in password_list:
            f.write(pwd + '\n')
    print(f"💾 Saved to: passwords_custom.txt")
    
    return password_list

# ============================================================
# PDF TESTER
# ============================================================
def test_password(pdf_path, password):
    """Test if a password works"""
    try:
        reader = PdfReader(pdf_path, password=password)
        return True
    except:
        return False

def save_unlocked(pdf_path, output_path, password):
    """Save the unlocked PDF"""
    try:
        reader = PdfReader(pdf_path, password=password)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        with open(output_path, 'wb') as f:
            writer.write(f)
        return True
    except:
        return False

# ============================================================
# MAIN CRACKER
# ============================================================
def crack_pdf(pdf_path, output_path):
    """Main cracking function"""
    
    print("\n" + "=" * 70)
    print("🚀 STARTING PDF CRACKER")
    print("=" * 70)
    print(f"📁 File: {pdf_path}")
    print(f"📄 Output: {output_path}")
    print("=" * 70)
    
    # Check file exists
    if not os.path.isfile(pdf_path):
        print(f"❌ File not found: {pdf_path}")
        return False
    
    # Generate passwords
    passwords = generate_all_passwords()
    
    if not passwords:
        print("❌ No passwords generated!")
        return False
    
    # ============================================================
    # QUICK SCAN - Try most obvious first
    # ============================================================
    print("\n⚡ QUICK SCAN - Trying most likely passwords...")
    print("-" * 60)
    
    quick_passwords = [
        ACCOUNT,
        f"{DAY}{MONTH}{YEAR}",
        f"{MONTH}{DAY}{YEAR}",
        PHONE[-10:],
        ACCOUNT[-4:],
        f"{ACCOUNT}{DAY}{MONTH}{YEAR}",
        f"{DAY}{MONTH}{YEAR}{ACCOUNT}",
        f"BANK{ACCOUNT}",
        f"{ACCOUNT}BANK",
        f"ACC{ACCOUNT}",
        f"{ACCOUNT}@123",
        f"@{ACCOUNT}",
        f"{ACCOUNT}!",
        f"{DAY}-{MONTH}-{YEAR}",
        f"{ACCOUNT[-4:]}{DAY}{MONTH}{YEAR}",
    ]
    
    for i, pwd in enumerate(quick_passwords, 1):
        print(f"[{i}] Trying: {pwd}")
        if test_password(pdf_path, pwd):
            print(f"\n✅ SUCCESS! Password found: {pwd}")
            if save_unlocked(pdf_path, output_path, pwd):
                print(f"📁 Unlocked PDF saved as: {output_path}")
                return True
            return True
    
    print("   ❌ Quick scan failed. Starting full attack...")
    
    # ============================================================
    # FULL ATTACK - Try ALL passwords
    # ============================================================
    print("\n🔓 FULL ATTACK - Testing all combinations...")
    print("-" * 60)
    print(f"📊 Total passwords to try: {len(passwords):,}")
    print("-" * 60)
    
    start_time = time.time()
    tried = 0
    
    for i, pwd in enumerate(passwords, 1):
        tried = i
        
        # Show progress
        if i % 1000 == 0:
            elapsed = time.time() - start_time
            speed = i / elapsed if elapsed > 0 else 0
            percent = (i / len(passwords)) * 100
            print(f"[{i:,}/{len(passwords):,}] {percent:.1f}% | {speed:.0f} pwd/sec | {pwd[:25]}...")
        
        # Test password
        if test_password(pdf_path, pwd):
            elapsed = time.time() - start_time
            print("\n" + "=" * 70)
            print("🎉 SUCCESS! PASSWORD FOUND!")
            print("=" * 70)
            print(f"🔓 Password: {pwd}")
            print(f"📊 Attempts: {tried:,}")
            print(f"⏱️  Time: {int(elapsed)} seconds")
            print("=" * 70)
            
            if save_unlocked(pdf_path, output_path, pwd):
                print(f"✅ Unlocked PDF saved as: {output_path}")
                return True
            
            return True
    
    # ============================================================
    # FAILED
    # ============================================================
    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print("❌ CRACKING FAILED")
    print("=" * 70)
    print(f"📊 Tried: {tried:,} passwords")
    print(f"⏱️  Time: {int(elapsed)} seconds")
    print("\n💡 The password is NOT based on your personal information.")
    print("\nPossible next steps:")
    print("  1. Contact your bank for the password")
    print("  2. Check your email/SMS for the password")
    print("  3. Try your ATM PIN (4-6 digits)")
    print("  4. Try your Customer ID")
    print("  5. Try your PAN card number")
    print("  6. Try your Aadhar number")
    print("=" * 70)
    
    return False

# ============================================================
# MAIN
# ============================================================
def main():
    print("""
╔═══════════════════════════════════════════════════════════════╗
║   ULTIMATE PDF CRACKER - 100% FOCUSED ON YOUR DATA           ║
╠═══════════════════════════════════════════════════════════════╣
║   Account: 2294641279                                        ║
║   DOB:     23-07-1998                                        ║
║   Phone:   09156084052                                       ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # Get input
    pdf_file = input("📁 Enter path to locked PDF: ").strip()
    
    if not pdf_file:
        print("❌ No file provided")
        return
    
    output_file = input("📄 Output path (Enter for 'unlocked.pdf'): ").strip()
    if not output_file:
        output_file = "unlocked.pdf"
    
    # Crack it
    crack_pdf(pdf_file, output_file)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ Process interrupted by user")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()