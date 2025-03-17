import os
import tempfile
from io import BytesIO
import logging

from pyrogram import Client, filters
from pyrogram.types import Message
from PyPDF2 import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import red, black, white

import pytesseract
from PIL import Image, ImageDraw, ImageFont
import fitz  # PyMuPDF

from config import BOT_TOKEN, API_ID, API_HASH, TESSERACT_CMD
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# Set up logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Conversation states
WAITING_FOR_PDF = "WAITING_FOR_PDF"
WAITING_FOR_LOCATION = "WAITING_FOR_LOCATION"
WAITING_FOR_FIND_TEXT = "WAITING_FOR_FIND_TEXT"  # For OCR Cover-Up (option 9)
WAITING_FOR_SIDE_TOP_LEFT = "WAITING_FOR_SIDE_TOP_LEFT"  # For Sides Cover-Up (option 10)
WAITING_FOR_SIDE_BOTTOM_RIGHT = "WAITING_FOR_SIDE_BOTTOM_RIGHT"
WAITING_FOR_WATERMARK_TEXT = "WAITING_FOR_WATERMARK_TEXT"
WAITING_FOR_TEXT_SIZE = "WAITING_FOR_TEXT_SIZE"
WAITING_FOR_COLOR = "WAITING_FOR_COLOR"

# Global dictionary to store conversation data per chat.
user_data = {}

def normalized_to_pdf_coords(norm_coord, page_width, page_height):
    """
    Converts a normalized coordinate (v,h) on a 0–10 scale into actual PDF coordinates.
    Here:
      - v: vertical coordinate (0 at top, 10 at bottom)
      - h: horizontal coordinate (0 at left, 10 at right)
    PDF coordinates have origin at bottom-left, so:
      PDF_x = (h/10) * page_width
      PDF_y = page_height - ((v/10) * page_height)
    """
    v, h = norm_coord
    pdf_x = (h / 10) * page_width
    pdf_y = page_height - ((v / 10) * page_height)
    logger.debug("Normalized coord %s converted to PDF coords: (%s, %s)", norm_coord, pdf_x, pdf_y)
    return (pdf_x, pdf_y)

def annotate_first_page_image(pdf_path, dpi=150):
    """
    Opens the PDF's first page using PyMuPDF, renders it as an image,
    and draws a blue border with tick marks and normalized coordinate labels (0–10)
    along the top (x axis) and left (y axis) edges.
    Returns the path of the annotated image.
    """
    logger.info("Annotating first page of PDF: %s", pdf_path)
    doc = fitz.open(pdf_path)
    page = doc[0]
    scale = dpi / 72
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
    
    img_width, img_height = image.size

    # Draw blue border.
    draw.rectangle([0, 0, img_width-1, img_height-1], outline="blue", width=2)
    
    # Draw tick marks along the top edge (horizontal scale 0 to 10).
    for i in range(11):
        x = (i/10) * img_width
        draw.line([(x, 0), (x, 10)], fill="blue", width=2)
        draw.text((x+2, 12), f"{i}", fill="blue", font=font)
    
    # Draw tick marks along the left edge (vertical scale 0 to 10).
    for i in range(11):
        y = (i/10) * img_height
        draw.line([(0, y), (10, y)], fill="blue", width=2)
        draw.text((12, y-6), f"{i}", fill="blue", font=font)
    
    annotated_path = pdf_path.replace(".pdf", "_annotated.jpg")
    image.save(annotated_path)
    doc.close()
    logger.info("Annotated image saved: %s", annotated_path)
    return annotated_path

async def send_first_page_image(client: Client, chat_id: int):
    """
    Downloads the first PDF from the user's list, creates an annotated image of its first page
    with a normalized grid from 0 to 10, and sends it to the user.
    """
    try:
        logger.info("Preparing annotated image for chat %s", chat_id)
        pdf_info = user_data[chat_id]["pdfs"][0]
        temp_pdf_path = os.path.join(tempfile.gettempdir(), pdf_info["file_name"])
        logger.info("Downloading PDF %s for chat %s", pdf_info["file_name"], chat_id)
        await client.download_media(pdf_info["file_id"], file_name=temp_pdf_path)
        annotated_path = annotate_first_page_image(temp_pdf_path, dpi=150)
        await client.send_photo(
            chat_id,
            photo=annotated_path,
            caption=("This image shows a normalized grid:\n"
                     "• Top edge: horizontal (x) scale: 0 (left) to 10 (right)\n"
                     "• Left edge: vertical (y) scale: 0 (top) to 10 (bottom)\n\n"
                     "Please provide two normalized coordinates in the format 'v,h' (values between 0 and 10):\n"
                     "• LEFT TOP (e.g., 2,3)   [v=2, h=3]\n"
                     "• RIGHT BOTTOM (e.g., 8,7)   [v=8, h=7]")
        )
        os.remove(temp_pdf_path)
        os.remove(annotated_path)
        logger.info("Temporary files removed for chat %s", chat_id)
    except Exception as e:
        logger.error("Error sending annotated image for chat %s: %s", chat_id, e)
        await client.send_message(chat_id, f"Error sending annotated image: {e}")

def create_watermarked_pdf(input_pdf_path, watermark_text, text_size, color, location, find_text=None, cover_coords=None):
    """
    For locations 1-8: standard watermark.
    For location 9 (OCR Cover-Up): uses PyMuPDF + pytesseract to cover found text.
    For location 10 (Sides Cover-Up): uses two normalized coordinates (v,h on 0–10 scale)
    to determine a rectangular region on each page, covers it with white,
    and places the watermark text centered in that region.
    """
    logger.info("Creating watermarked PDF for: %s", input_pdf_path)
    if location == 9 and find_text:
        logger.info("Using OCR Cover-Up for text: %s", find_text)
        doc = fitz.open(input_pdf_path)
        dpi = 150
        scale = dpi / 72
        for page in doc:
            page_width = page.rect.width
            page_height = page.rect.height
            mat = fitz.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            n_boxes = len(ocr_data["text"])
            for i in range(n_boxes):
                word = ocr_data["text"][i].strip()
                if word.lower() == find_text.lower():
                    left = ocr_data["left"][i]
                    top = ocr_data["top"][i]
                    width = ocr_data["width"][i]
                    height = ocr_data["height"][i]
                    pdf_x = left / scale
                    pdf_width = width / scale
                    pdf_height = height / scale
                    pdf_y = page_height - ((top + height) / scale)
                    rect = fitz.Rect(pdf_x, pdf_y, pdf_x + pdf_width, pdf_y + pdf_height)
                    page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))
                    text_x = pdf_x
                    text_y = pdf_y + pdf_height + 2
                    wm_color = (color.red, color.green, color.blue)
                    page.insert_text((text_x, text_y), watermark_text, fontsize=text_size, color=wm_color)
                    logger.debug("Applied OCR watermark on word '%s' at rect %s", word, rect)
        output_pdf_path = input_pdf_path.replace(".pdf", "_watermarked.pdf")
        doc.save(output_pdf_path)
        logger.info("OCR watermarked PDF saved: %s", output_pdf_path)
        return output_pdf_path

    elif location == 10 and cover_coords and len(cover_coords) == 2:
        logger.info("Using Sides Cover-Up with coordinates: %s", cover_coords)
        # Sides Cover-Up applied to every page.
        doc = fitz.open(input_pdf_path)
        for page in doc:
            page_width = page.rect.width
            page_height = page.rect.height
            left_top_pdf = normalized_to_pdf_coords(cover_coords[0], page_width, page_height)
            right_bottom_pdf = normalized_to_pdf_coords(cover_coords[1], page_width, page_height)
            x1, y1 = left_top_pdf
            x2, y2 = right_bottom_pdf
            rect = fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1))
            center_x = (rect.x0 + rect.x1) / 2
            center_y = (rect.y0 + rect.y1) / 2
            wm_color = (color.red, color.green, color.blue)
            text_box = fitz.Rect(center_x - 100, center_y - text_size, center_x + 100, center_y + text_size)
            page.insert_textbox(text_box, watermark_text, fontsize=text_size, color=wm_color, align=1)
            logger.debug("Applied Sides Cover-Up on page with rect: %s", rect)
        output_pdf_path = input_pdf_path.replace(".pdf", "_watermarked.pdf")
        doc.save(output_pdf_path)
        logger.info("Sides watermarked PDF saved: %s", output_pdf_path)
        return output_pdf_path

    else:
        logger.info("Using standard watermarking method.")
        # Standard watermarking using ReportLab and PyPDF2.
        reader = PdfReader(input_pdf_path)
        first_page = reader.pages[0]
        page_width = float(first_page.mediabox.width)
        page_height = float(first_page.mediabox.height)
        watermark_stream = BytesIO()
        c = canvas.Canvas(watermark_stream, pagesize=(page_width, page_height))
        c.setFont("Helvetica", text_size)
        c.setFillColor(color)
        margin = 10

        x, y = 0, 0
        rotation = 0
        if location == 1:
            x = page_width - margin - 100
            y = page_height - margin - text_size
        elif location == 2:
            x = (page_width / 2) - 50
            y = page_height - margin - text_size
        elif location == 3:
            x = margin
            y = page_height - margin - text_size
        elif location == 4:
            x = (page_width / 2) - 50
            y = (page_height / 2) - (text_size / 2)
        elif location == 5:
            x = (page_width / 2) - 50
            y = (page_height / 2) - (text_size / 2)
            rotation = 45
        elif location == 6:
            x = page_width - margin - 100
            y = margin
        elif location == 7:
            x = (page_width / 2) - 50
            y = margin
        elif location == 8:
            x = margin
            y = margin

        if rotation:
            c.saveState()
            c.translate(x, y)
            c.rotate(rotation)
            c.drawString(0, 0, watermark_text)
            c.restoreState()
        else:
            c.drawString(x, y, watermark_text)
        c.save()
        watermark_stream.seek(0)
        watermark_reader = PdfReader(watermark_stream)
        watermark_page = watermark_reader.pages[0]
        writer = PdfWriter()
        for page in reader.pages:
            page.merge_page(watermark_page)
            writer.add_page(page)
        output_pdf_path = input_pdf_path.replace(".pdf", "_watermarked.pdf")
        with open(output_pdf_path, "wb") as out_file:
            writer.write(out_file)
        logger.info("Standard watermarked PDF saved: %s", output_pdf_path)
        return output_pdf_path

async def process_pdfs_handler(client: Client, chat_id: int):
    data = user_data.get(chat_id)
    if not data:
        logger.warning("No data found for chat %s", chat_id)
        return
    pdfs = data.get("pdfs", [])
    location = data.get("location")
    watermark_text = data.get("watermark_text")
    text_size = data.get("text_size")
    color_name = data.get("color")
    color_mapping = {"red": red, "black": black, "white": white}
    watermark_color = color_mapping.get(color_name, black)
    
    find_text = data.get("find_text") if location == 9 else None
    cover_coords = data.get("side_coords") if location == 10 else None

    logger.info("Processing %d PDF(s) for chat %s", len(pdfs), chat_id)
    for pdf_info in pdfs:
        file_id = pdf_info["file_id"]
        file_name = pdf_info["file_name"]
        try:
            temp_pdf_path = os.path.join(tempfile.gettempdir(), file_name)
            logger.info("Downloading PDF %s for chat %s", file_name, chat_id)
            await client.download_media(file_id, file_name=temp_pdf_path)
        except Exception as e:
            logger.error("Error downloading %s for chat %s: %s", file_name, chat_id, e)
            await client.send_message(chat_id, f"Error downloading {file_name}: {e}")
            continue

        watermarked_pdf_path = create_watermarked_pdf(
            temp_pdf_path, watermark_text, text_size, watermark_color,
            location, find_text=find_text, cover_coords=cover_coords
        )
        try:
            logger.info("Sending watermarked PDF %s for chat %s", watermarked_pdf_path, chat_id)
            await client.send_document(chat_id, watermarked_pdf_path)
        except Exception as e:
            logger.error("Error sending watermarked file %s for chat %s: %s", file_name, chat_id, e)
            await client.send_message(chat_id, f"Error sending watermarked file {file_name}: {e}")
        try:
            os.remove(temp_pdf_path)
            os.remove(watermarked_pdf_path)
            logger.info("Removed temporary files for %s", file_name)
        except Exception as e:
            logger.warning("Error removing temporary files for %s: %s", file_name, e)

app = Client("pdf_watermark_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

@app.on_message(filters.command("pdfwatermark"))
async def start_pdfwatermark_handler(client: Client, message: Message):
    chat_id = message.chat.id
    user_data[chat_id] = {"state": WAITING_FOR_PDF, "pdfs": []}
    logger.info("Chat %s started PDF watermarking.", chat_id)
    await message.reply_text("Please send all PDF files now.")

@app.on_message(filters.document)
async def receive_pdf_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in user_data or user_data[chat_id].get("state") != WAITING_FOR_PDF:
        return
    document = message.document
    if document.mime_type != "application/pdf":
        logger.info("Received non-PDF file in chat %s", chat_id)
        await message.reply_text("This is not a PDF file. Please send a PDF.")
        return
    user_data[chat_id]["pdfs"].append({
        "file_id": document.file_id,
        "file_name": document.file_name
    })
    logger.info("Received PDF %s for chat %s", document.file_name, chat_id)
    await message.reply_text(f"Received {document.file_name}. You can send more PDFs or type /pdfask when done.")

@app.on_message(filters.command("pdfask"))
async def start_pdfask_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in user_data or not user_data[chat_id].get("pdfs"):
        logger.warning("No PDFs found for chat %s when /pdfask was invoked", chat_id)
        await message.reply_text("No PDFs received. Please start with /pdfwatermark and then send PDF files.")
        return
    user_data[chat_id]["state"] = WAITING_FOR_LOCATION
    logger.info("Chat %s moving to watermark location selection.", chat_id)
    await message.reply_text(
        "Choose watermark location by sending a number:\n"
        "1. Top right\n"
        "2. Top middle\n"
        "3. Top left\n"
        "4. Middle straight\n"
        "5. Middle 45 degree\n"
        "6. Bottom right\n"
        "7. Bottom centre\n"
        "8. Bottom left\n"
        "9. Cover-Up (using OCR)\n"
        "10. Sides Cover-Up (rectangle with normalized 0-10 coordinates)"
    )

@app.on_message(filters.text & ~filters.command(["pdfwatermark", "pdfask"]))
async def handle_text_handler(client: Client, message: Message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        logger.debug("No active session for chat %s", chat_id)
        return
    state = user_data[chat_id].get("state")
    text = message.text.strip()
    
    if state == WAITING_FOR_LOCATION:
        try:
            loc = int(text)
            if loc < 1 or loc > 10:
                logger.warning("Invalid location choice (%s) in chat %s", text, chat_id)
                await message.reply_text("Invalid choice. Please send a number between 1 and 10 for location.")
                return
        except ValueError:
            logger.warning("Non-integer location choice in chat %s: %s", chat_id, text)
            await message.reply_text("Please send a valid number for location.")
            return
        user_data[chat_id]["location"] = loc
        logger.info("Chat %s selected location %s", chat_id, loc)
        if loc == 9:
            user_data[chat_id]["state"] = WAITING_FOR_FIND_TEXT
            await message.reply_text("Enter the text to find (the text you want to cover up):")
        elif loc == 10:
            await send_first_page_image(client, chat_id)
            user_data[chat_id]["state"] = WAITING_FOR_SIDE_TOP_LEFT
            await message.reply_text("Enter the LEFT TOP normalized coordinate (format: x,y in 0-10, e.g., 2,3):")
        else:
            user_data[chat_id]["state"] = WAITING_FOR_WATERMARK_TEXT
            await message.reply_text("Enter watermark text:")
    elif state == WAITING_FOR_FIND_TEXT:
        if not text:
            await message.reply_text("Text to find cannot be empty. Please enter the text to cover up:")
            return
        user_data[chat_id]["find_text"] = text
        logger.info("Chat %s provided OCR text to find: %s", chat_id, text)
        user_data[chat_id]["state"] = WAITING_FOR_WATERMARK_TEXT
        await message.reply_text("Enter watermark text:")
    elif state == WAITING_FOR_SIDE_TOP_LEFT:
        try:
            x_str, y_str = text.split(",")
            coord = (float(x_str.strip()), float(y_str.strip()))
        except Exception:
            logger.warning("Invalid LEFT TOP coordinate in chat %s: %s", chat_id, text)
            await message.reply_text("Invalid format. Please enter coordinate as x,y (e.g., 2,3).")
            return
        user_data[chat_id]["side_coords"] = [coord]
        logger.info("Chat %s received LEFT TOP coordinate: %s", chat_id, coord)
        user_data[chat_id]["state"] = WAITING_FOR_SIDE_BOTTOM_RIGHT
        await message.reply_text("Enter the RIGHT BOTTOM normalized coordinate (format: x,y in 0-10, e.g., 8,7):")
    elif state == WAITING_FOR_SIDE_BOTTOM_RIGHT:
        try:
            x_str, y_str = text.split(",")
            coord = (float(x_str.strip()), float(y_str.strip()))
        except Exception:
            logger.warning("Invalid RIGHT BOTTOM coordinate in chat %s: %s", chat_id, text)
            await message.reply_text("Invalid format. Please enter coordinate as x,y (e.g., 8,7).")
            return
        user_data[chat_id]["side_coords"].append(coord)
        logger.info("Chat %s received RIGHT BOTTOM coordinate: %s", chat_id, coord)
        user_data[chat_id]["state"] = WAITING_FOR_WATERMARK_TEXT
        await message.reply_text("Enter watermark text:")
    elif state == WAITING_FOR_WATERMARK_TEXT:
        if not text:
            await message.reply_text("Watermark text cannot be empty. Please enter the watermark text.")
            return
        user_data[chat_id]["watermark_text"] = text
        logger.info("Chat %s provided watermark text: %s", chat_id, text)
        user_data[chat_id]["state"] = WAITING_FOR_TEXT_SIZE
        await message.reply_text("Enter watermark text size (e.g., 24):")
    elif state == WAITING_FOR_TEXT_SIZE:
        try:
            size = int(text)
        except ValueError:
            logger.warning("Invalid text size provided in chat %s: %s", chat_id, text)
            await message.reply_text("Please send a valid number for text size.")
            return
        user_data[chat_id]["text_size"] = size
        logger.info("Chat %s set text size to: %s", chat_id, size)
        user_data[chat_id]["state"] = WAITING_FOR_COLOR
        await message.reply_text("Choose watermark text colour by sending a number:\n1. Red\n2. Black\n3. White")
    elif state == WAITING_FOR_COLOR:
        mapping = {"1": "red", "2": "black", "3": "white"}
        if text not in mapping:
            logger.warning("Invalid colour choice in chat %s: %s", chat_id, text)
            await message.reply_text("Invalid choice. Please choose 1, 2, or 3 for colour.")
            return
        user_data[chat_id]["color"] = mapping[text]
        logger.info("Chat %s set colour to: %s", chat_id, mapping[text])
        await message.reply_text("PDF watermarking started.")
        await process_pdfs_handler(client, chat_id)
        logger.info("PDF processing completed for chat %s", chat_id)
        user_data.pop(chat_id, None)

@app.on_message(filters.text & ~filters.command(["pdfwatermark", "pdfask"]))
async def extra_text_handler(client: Client, message: Message):
    # This handler is here if any extra text comes in while no state is active.
    logger.debug("Extra text received in chat %s: %s", message.chat.id, message.text)

app.run()
