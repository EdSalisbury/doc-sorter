import os
import openai
from dotenv import load_dotenv
import json
import shutil
import re
import pypdf
import base64
from io import BytesIO
from PIL import Image
import pypdfium2
import sys

load_dotenv()


# Retrieve the prompt and replace escaped newlines
DOCUMENT_PROMPT = os.getenv("DOCUMENT_PROMPT", "").replace("\\n", "\n")

# Set up OpenAI API key
openai.api_key = os.environ.get("OPENAI_API_KEY")

# Define directories
INPUT_DIR = "D:\\Documents\\Incoming"  # Where your PDFs are stored
OUTPUT_DIR = "D:\\Documents\\Filed"  # Where renamed PDFs will go

# Ensure output directory exists
os.makedirs(OUTPUT_DIR, exist_ok=True)


def extract_text_with_pypdf(pdf_path):
    """Extracts text from a PDF using PyPDF (fast, free)."""
    print(f"🔍 Trying PyPDF to extract data from {pdf_path}...")
    try:
        with open(pdf_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            extracted_text = "\n".join(
                [page.extract_text() or "" for page in reader.pages]
            ).strip()
            return extracted_text
    except Exception as e:
        print(f"🚨 pypdf extraction failed for {pdf_path}: {e}")
        return ""


def encode_image(image):
    """Convert a PIL image to a base64-encoded string for OpenAI."""
    buffered = BytesIO()
    image.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode("utf-8")


def extract_text_with_gpt4_vision(pdf_path):
    """Extracts text from a scanned PDF using GPT-4 Vision with a single stacked image."""
    print(f"🔍 Trying GPT4 for {pdf_path}...")
    try:
        pdf = pypdfium2.PdfDocument(pdf_path)  # Open PDF
        images = [
            page.render(scale=2.0).to_pil() for page in pdf
        ]  # Convert pages to images
        pdf.close()  # ✅ Explicitly close the file to prevent Windows file locks

        if not images:
            print(f"🚨 No images rendered for {pdf_path}. Skipping.")
            return ""

        # Stack images vertically into one single image
        total_width = max(img.width for img in images)
        total_height = sum(img.height for img in images)
        combined_image = Image.new("RGB", (total_width, total_height))

        y_offset = 0
        for img in images:
            combined_image.paste(img, (0, y_offset))
            y_offset += img.height  # Move down for the next page

        # Convert combined image to base64
        encoded_image = encode_image(combined_image)

        client = openai.OpenAI()

        # Send to OpenAI GPT-4 Vision (Latest API)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "Extract all printed and handwritten text from this document.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract text from this document."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{"image/png"};base64,{encoded_image}"
                            },
                        },
                    ],
                },
            ],
        )

        # Extract text from OpenAI response
        extracted_text = response.choices[0].message.content.strip()
        return extracted_text

    except Exception as e:
        print(f"🚨 GPT-4 Vision OCR failed for {pdf_path}: {e}")
        return ""


def redact_sensitive_info(text):
    """Remove sensitive data from text before sending it to OpenAI."""
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED SSN]", text)
    text = re.sub(r"\b\d{4}-\d{4}-\d{4}-\d{4}\b", "[REDACTED CARD]", text)
    text = re.sub(r"\b\d{9}\b", "[REDACTED ACCOUNT]", text)
    return text


# def extract_text_from_pdf(pdf_path):
#     """Hybrid PDF OCR: Tries PyPDF first, falls back to GPT-4 Vision if needed."""
#     print(f"🔍 Trying pypdf for {pdf_path}...")
#     extracted_text = extract_text_with_pypdf(pdf_path)

#     if (
#         extracted_text and len(extracted_text) > 50
#     ):  # Ensures text isn't empty or too short
#         print(f"✅ pypdf succeeded for {pdf_path} (Skipping GPT-4 Vision)")
#         return extracted_text
#     else:
#         print(
#             f"⚠️ pypdf failed or returned bad data for {pdf_path} (Using GPT-4 Vision)..."
#         )
#         return extract_text_with_gpt4_vision(pdf_path)


def get_unique_filename(directory, filename):
    """Ensure a unique filename by adding a counter if a duplicate exists."""
    base_name, ext = os.path.splitext(filename)
    counter = 1
    new_filename = filename

    while os.path.exists(os.path.join(directory, new_filename)):
        new_filename = f"{base_name}-{counter}{ext}"
        counter += 1

    return new_filename


def get_llm_metadata(text):
    """Sends extracted text to the LLM and returns structured metadata."""
    try:
        # Inject PDF text into the prompt
        prompt = DOCUMENT_PROMPT.replace("{PDF_TEXT}", text[:3000])

        client = openai.OpenAI()
        response = client.chat.completions.create(
            model="gpt-4-turbo", messages=[{"role": "system", "content": prompt}]
        )

        try:
            result = json.loads(response.choices[0].message.content)
            return result  # Return structured data directly
        except json.JSONDecodeError:
            print("Error parsing LLM response. Output from LLM:")
            print(response.choices[0].message.content)
            return None

    except Exception as e:
        print(f"🚨 Error communicating with LLM: {e}")
        return None


def is_unreadable(ai_response):
    """Returns True if the LLM marked the document as unreadable."""
    return ai_response.get("category", "").lower() == "unreadable"


def move_pdf(file_path, ai_response):
    """Moves and renames a processed PDF file based on LLM response."""
    category = ai_response["category"]
    date = ai_response["date"]
    new_filename = ai_response["filename"]

    # Extract the year from the date
    year = date[:4] if date and date != "unknown" else "Unkn"

    # Construct new directory structure
    year_dir = os.path.join(OUTPUT_DIR, year, f"{year} - {category}")
    os.makedirs(year_dir, exist_ok=True)

    # Define final file path
    unique_filename = get_unique_filename(year_dir, new_filename)
    new_file_path = os.path.join(year_dir, unique_filename)

    # Move and rename the file
    shutil.move(file_path, new_file_path)
    print(f"✅ Moved to {new_file_path}")


def process_pdfs():
    """Process all PDFs in the input directory but skip unreadable ones."""
    for filename in os.listdir(INPUT_DIR):
        if filename.lower().endswith(".pdf"):
            file_path = os.path.join(INPUT_DIR, filename)
            process_pdf(file_path)
            print()
        else:
            print(f"Skipping {filename}...")


def process_pdf(filename):
    print(f"Processing {filename}...")

    try:
        # Get LLM response
        text = extract_text_with_pypdf(filename)
        ai_response = get_llm_metadata(text)
        if not ai_response:
            print(f"⚠️ Skipping {filename} due to LLM error.")
            return None

        if (
            "unknown" in str(ai_response).lower()
            or "unidentified" in str(ai_response).lower()
        ):
            print(ai_response)
            text = extract_text_with_gpt4_vision(filename)
            ai_response = get_llm_metadata(text)

        # Check if the file is unreadable and should be skipped
        if is_unreadable(ai_response):
            print(f"⚠️ LLM marked {filename} as unreadable. Skipping.")
            return None

        # Move and rename the file
        move_pdf(filename, ai_response)

    except Exception as e:
        print(f"🚨 Error processing {filename}: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        process_pdf(sys.argv[1])
    else:
        process_pdfs()
