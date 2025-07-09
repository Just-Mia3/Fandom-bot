import os
import json
import gspread
import requests
import mwclient
import re
import time
from io import BytesIO
from PIL import Image
from urllib.parse import quote
from oauth2client.service_account import ServiceAccountCredentials

# === 1. Write Google service key to creds.json ===
key_content = os.getenv("KEY")
with open("creds.json", "w") as f:
    f.write(key_content)
print("🔐 Wrote creds.json from secret")

# === 2. Set up Google Sheets ===
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
gc = gspread.authorize(creds)
print("📗 Connected to Google Sheets")

# === 3. Open the spreadsheet and both sheets ===
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1NL8ksUmy_Yhkjvhk3oNra5Mj-pvp82JTFEmceM-mdeA/edit"
spreadsheet = gc.open_by_url(SPREADSHEET_URL)
upload_sheet = spreadsheet.worksheet("Upload")
content_sheet = spreadsheet.worksheet("Content")
print("📄 Loaded 'Upload' and 'Content' sheets")

# === 4. Login to Fandom using mwclient ===
USERNAME = os.getenv("USER")
PASSWORD = os.getenv("PASSWORD")
site = mwclient.Site('gacha-designer.fandom.com', path='/')
site.login(USERNAME, PASSWORD)
print(f"✅ Logged in as {USERNAME}")

# Get API token and cookies
edit_token = site.get_token('csrf')
cookies = site.connection.cookies.get_dict()
print("📎 Retrieved CSRF token and cookies for Fandom")

# === 5. Load sheet data ===
upload_records = upload_sheet.get_all_records(expected_headers=[
    "Image", "Page", "Type", "Number", "Asset Designer", "Layers", "Process", "Reason"
])
content_records = content_sheet.get_all_records()
type_map = {
    row["Type"]: row["Number"]
    for row in content_records if isinstance(row["Number"], int)
}
print(f"📥 Found {len(upload_records)} rows to process")

# === 6. Process each upload ===
for i, row in enumerate(upload_records):
    row_index = i + 2
    process = row.get("Process", "").strip().lower()
    if process in ("successful", "skip", "hold"):
        print(f"⏭️ Row {row_index}: Skipping due to process status: {process}")
        continue

    image_url = row["Image"]
    page_name = row["Page"]
    asset_type = row["Type"]
    designer = row["Asset Designer"]
    layers = row.get("Layers", "")
    ignore_warnings = process == "failed"

    if asset_type not in type_map:
        reason = f"No number found for type: {asset_type}"
        print(f"❌ Row {row_index}: {reason}")
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, reason)
        continue

    number = str(type_map[asset_type])
    upload_sheet.update_cell(row_index, 4, number)
    filename = f"{asset_type}{number}.png"
    local_path = f"/tmp/{filename}"

    print(f"\n🔄 Row {row_index}: Preparing file {filename}...")

    # === Download and compress image ===
    try:
        print(f"🌐 Downloading: {image_url}")
        res = requests.get(image_url)
        res.raise_for_status()
        print(f"📦 Original image size: {len(res.content) // 1024} KB")
        img = Image.open(BytesIO(res.content)).convert("RGBA")
        img.thumbnail((1024, 1024))

        buffer = BytesIO()
        img.save(buffer, format="PNG")

        size_limit = 500 * 1024
        scale = 1.0
        while buffer.tell() > size_limit and scale > 0.2:
            scale -= 0.1
            width, height = img.size
            img = img.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            print(f"🔧 Compressed to {img.size}, now {buffer.tell() // 1024} KB")

        with open(local_path, "wb") as f:
            f.write(buffer.getvalue())
        print(f"✅ Final image saved: {buffer.tell() // 1024} KB")

    except Exception as e:
        reason = f"Image error: {e}"
        print(f"❌ {reason}")
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, reason)
        continue

    # === Build image description ===
    description = f"Made by {designer}"
    if layers.strip():
        layer_links = [link.strip() for link in layers.split(",") if link.strip()]
        if layer_links:
            description += "\n\n**Layer Images:**"
            for i, link in enumerate(layer_links[:5], 1):
                description += f"\n[{i}]({link})"

    # === Upload image ===
    print(f"⬆️ Uploading {filename} to Fandom...")
    try:
        with open(local_path, 'rb') as f:
            files = {
                'file': (filename, f, 'image/png'),
            }
            data = {
                'action': 'upload',
                'filename': filename,
                'format': 'json',
                'token': edit_token,
                'comment': description,
                'ignorewarnings': '1' if ignore_warnings else '0',
            }
            headers = {
                'User-Agent': 'RenderUploaderBot/1.0',
                'Connection': 'keep-alive'
            }

            try:
                response = requests.post(
                    'https://gacha-designer.fandom.com/api.php',
                    data=data,
                    files=files,
                    headers=headers,
                    cookies=cookies,
                    timeout=60
                )
                response.raise_for_status()
            except requests.exceptions.ConnectionError:
                print("⚠️ Connection dropped — retrying in 5 seconds...")
                time.sleep(5)
                response = requests.post(
                    'https://gacha-designer.fandom.com/api.php',
                    data=data,
                    files=files,
                    headers=headers,
                    cookies=cookies,
                    timeout=60
                )
                response.raise_for_status()
                print(f"✅ Retry worked for {filename}")

            result = response.json()
            if "error" in result:
                raise Exception(result["error"].get("info", "Unknown upload error"))
            print(f"✅ Successfully uploaded {filename}")

    except Exception as e:
        reason = f"Upload error: {e}"
        print(f"❌ {reason}")
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, reason)
        continue

    # === Insert into gallery ===
    try:
        print(f"🧩 Inserting {filename} into gallery on page: {page_name}")
        page = site.pages[page_name]
        text = page.text()
        caption = description
        section_match = re.search(rf"(==+\s*{re.escape(asset_type)}\s*==+)", text, re.IGNORECASE)
        if not section_match:
            raise Exception(f"Missing =={asset_type}== heading")
        section_start = section_match.end()
        gallery_match = re.search(r"<gallery>(.*?)</gallery>", text[section_start:], re.DOTALL)
        if not gallery_match:
            raise Exception("No <gallery> found under heading")

        g_start, g_end = gallery_match.span(1)
        g_offset = section_start
        g_text = gallery_match.group(1).strip()
        new_entry = f"\nFile:{filename}|{caption}"
        updated_gallery = g_text + new_entry
        new_text = text[:g_offset + g_start] + "\n" + updated_gallery + "\n" + text[g_offset + g_end:]
        page.save(new_text, summary=f"Added {filename} to gallery")
        print(f"📌 Added {filename} to {asset_type} section on {page_name}")

        upload_sheet.update_cell(row_index, 7, "Successful")
        upload_sheet.update_cell(row_index, 8, "")
        for r, cr in enumerate(content_records):
            if cr["Type"].strip() == asset_type:
                new_num = type_map[asset_type] + 1
                content_sheet.update_cell(r + 2, 2, new_num)
                type_map[asset_type] = new_num
                print(f"🔢 Incremented {asset_type} number to {new_num}")
                break
        time.sleep(2)

    except Exception as e:
        reason = f"Gallery error: {e}"
        print(f"❌ {reason}")
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, reason)
        continue

# === Cleanup ===
try:
    os.remove("creds.json")
    print("🗑️ Removed creds.json for security")
except:
    print("⚠️ Could not delete creds.json")
