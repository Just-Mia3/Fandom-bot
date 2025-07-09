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

# === 2. Set up Google Sheets ===
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("creds.json", scope)
gc = gspread.authorize(creds)

# === 3. Open the spreadsheet and both sheets ===
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1NL8ksUmy_Yhkjvhk3oNra5Mj-pvp82JTFEmceM-mdeA/edit"
spreadsheet = gc.open_by_url(SPREADSHEET_URL)
upload_sheet = spreadsheet.worksheet("Upload")
content_sheet = spreadsheet.worksheet("Content")

# === 4. Login to Fandom using mwclient for token ===
USERNAME = os.getenv("USER")
PASSWORD = os.getenv("PASSWORD")
site = mwclient.Site('gacha-designer.fandom.com', path='/')
site.login(USERNAME, PASSWORD)
print(f"✅ Logged in as {USERNAME}")

# Get API upload token
edit_token = site.get_token('csrf')
cookies = site.connection.cookies.get_dict()

# === 5. Read upload and content data ===
upload_records = upload_sheet.get_all_records(expected_headers=[
    "Image", "Page", "Type", "Number", "Asset Designer", "Layers", "Process",
    "Reason"
])
content_records = content_sheet.get_all_records()
type_map = {
    row["Type"]: row["Number"]
    for row in content_records if isinstance(row["Number"], int)
}

# === 6. Process Upload ===
for i, row in enumerate(upload_records):
    row_index = i + 2
    process = row.get("Process", "").strip().lower()
    if process in ("successful", "skip", "hold"):
        continue

    image_url = row["Image"]
    page_name = row["Page"]
    asset_type = row["Type"]
    designer = row["Asset Designer"]
    layers = row.get("Layers", "")
    ignore_warnings = process == "failed"

    if asset_type not in type_map:
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8,
                                 f"No number for type {asset_type}")
        continue

    number = str(type_map[asset_type])
    upload_sheet.update_cell(row_index, 4, number)
    filename = f"{asset_type}{number}.png"
    local_path = f"/tmp/{filename}"

    # === Download and process image ===
    try:
        res = requests.get(image_url)
        res.raise_for_status()
        img = Image.open(BytesIO(res.content)).convert("RGBA")
        img.thumbnail((1024, 1024))

        buffer = BytesIO()
        img.save(buffer, format="PNG")
        while buffer.tell() > 1 * 1024 * 1024:  # Compress until <1MB
            buffer = BytesIO()
            width, height = img.size
            img = img.resize((int(width * 0.9), int(height * 0.9)),
                             Image.LANCZOS)
            img.save(buffer, format="PNG")
        with open(local_path, "wb") as f:
            f.write(buffer.getvalue())
    except Exception as e:
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, f"Image error: {e}")
        continue

    # === Build description ===
    description = f"Made by {designer}"
    if layers.strip():
        layer_links = [
            link.strip() for link in layers.split(",") if link.strip()
        ]
        if layer_links:
            description += "\n\n**Layer Images:**"
            for i, link in enumerate(layer_links[:5], 1):
                description += f"\n[{i}]({link})"

    # === Upload via raw POST ===
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
            headers = {'User-Agent': 'ReplitUploaderBot/1.0'}
            response = requests.post(
                'https://gacha-designer.fandom.com/api.php',
                data=data,
                files=files,
                headers=headers,
                cookies=cookies,
                timeout=60)
        response.raise_for_status()
        result = response.json()
        if "error" in result:
            raise Exception(result["error"].get("info",
                                                "Unknown upload error"))
    except Exception as e:
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, f"Upload error: {e}")
        continue

    # === Insert into gallery ===
    try:
        page = site.pages[page_name]
        text = page.text()
        caption = description
        section_match = re.search(rf"(==+\s*{re.escape(asset_type)}\s*==+)",
                                  text, re.IGNORECASE)
        if not section_match:
            raise Exception(f"Missing =={asset_type}== heading")
        section_start = section_match.end()
        gallery_match = re.search(r"<gallery>(.*?)</gallery>",
                                  text[section_start:], re.DOTALL)
        if not gallery_match:
            raise Exception("No <gallery> found under heading")

        g_start, g_end = gallery_match.span(1)
        g_offset = section_start
        g_text = gallery_match.group(1).strip()
        new_entry = f"\nFile:{filename}|{caption}"
        updated_gallery = g_text + new_entry
        new_text = text[:g_offset +
                        g_start] + "\n" + updated_gallery + "\n" + text[
                            g_offset + g_end:]
        page.save(new_text, summary=f"Added {filename} to gallery")

        # Update success
        upload_sheet.update_cell(row_index, 7, "Successful")
        upload_sheet.update_cell(row_index, 8, "")
        for r, cr in enumerate(content_records):
            if cr["Type"].strip() == asset_type:
                new_num = type_map[asset_type] + 1
                content_sheet.update_cell(r + 2, 2, new_num)
                type_map[asset_type] = new_num
                break
        time.sleep(2)
    except Exception as e:
        upload_sheet.update_cell(row_index, 7, "Failed")
        upload_sheet.update_cell(row_index, 8, f"Gallery error: {e}")
        continue

# === Cleanup ===
try:
    os.remove("creds.json")
except:
    pass
