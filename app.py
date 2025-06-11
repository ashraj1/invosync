import os
import uuid
import json
import logging
import base64
import fitz  # PyMuPDF
import openai
from flask import Flask, request, render_template, flash, redirect, url_for
from werkzeug.utils import secure_filename
from PIL import Image
from azure.core.exceptions import HttpResponseError
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.storage.fileshare import ShareFileClient

# --- Configuration ---
# Azure
AZURE_ENDPOINT = os.environ.get("AZURE_ENDPOINT")
AZURE_KEY = os.environ.get("AZURE_KEY")
# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# Optional: Azure Storage for archiving
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
AZURE_SHARE_NAME = os.environ.get("AZURE_SHARE_NAME")

# --- Client Initialization ---
azure_di_client = DocumentIntelligenceClient(endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY)) if AZURE_ENDPOINT and AZURE_KEY else None
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("FLASK_SECRET_KEY", "a-strong-default-secret-key")
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'pdf'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
logging.basicConfig(level=logging.INFO)

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def safe_val(v):
    if v is None: return None
    if hasattr(v, "value_address") and v.value_address:
        addr = v.value_address
        parts = [getattr(addr, f) for f in ["house_number", "road", "city", "state", "postal_code", "country_region"] if getattr(addr, f, None)]
        return ", ".join(parts)
    if hasattr(v, "value_currency") and v.value_currency:
        cur = v.value_currency
        amount, symbol = getattr(cur, "amount", None), getattr(cur, "symbol", "") or getattr(cur, "currency_code", "")
        return f"{symbol}{amount}" if amount is not None else symbol
    if hasattr(v, "value_phone_number") and v.value_phone_number: return v.value_phone_number
    if hasattr(v, "value_array") and v.value_array: return ", ".join([safe_val(item) for item in v.value_array])
    if hasattr(v, "value_object") and v.value_object:
        parts = [f"{key}: {safe_val(value)}" for key, value in v.value_object.items() if safe_val(value)]
        return "; ".join(parts)
    for attr in ["value_string", "value_number", "value_date"]:
        if hasattr(v, attr):
            val = getattr(v, attr)
            if val is not None: return str(val)
    if hasattr(v, "content"): return v.content
    return str(v)

# --- Main Route ---
@app.route('/', methods=['GET', 'POST'])
def upload_invoice():
    if request.method == 'POST':
        if 'invoice' not in request.files:
            flash('No file part provided.')
            return redirect(request.url)
        file = request.files['invoice']
        parser_choice = request.form.get('parser_choice')
        if file.filename == '':
            flash('No file selected.')
            return redirect(request.url)

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_filename = f"{uuid.uuid4().hex}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            all_fields, line_items = {}, []

            try:
                file.save(filepath)
                
                if parser_choice == 'azure':
                    if not azure_di_client:
                        raise ValueError("Azure AI client is not configured. Check AZURE_ENDPOINT and AZURE_KEY.")
                    logging.info("Parsing with Azure AI prebuilt-invoice model...")
                    with open(filepath, "rb") as f:
                        poller = azure_di_client.begin_analyze_document("prebuilt-invoice", f)
                        result = poller.result()
                    if result.documents:
                        doc = result.documents[0]
                        for key, field in doc.fields.items():
                            if key == "Items":
                                if field.value_array:
                                    for item in field.value_array:
                                        item_fields, temp_item = item.value_object, {}
                                        for k in ["Description", "Quantity", "Unit", "UnitPrice", "ProductCode", "Tax", "Amount"]:
                                            temp_item[k] = safe_val(item_fields.get(k))
                                        line_items.append(temp_item)
                            else:
                                all_fields[key] = safe_val(field)
                
                elif parser_choice == 'openai':
                    if not openai_client:
                        raise ValueError("OpenAI client is not configured. Check OPENAI_API_KEY.")
                    logging.info("Parsing with OpenAI...")
                    image_path = filepath
                    if filepath.lower().endswith('.pdf'):
                        with fitz.open(filepath) as pdf_doc:
                            page = pdf_doc.load_page(0)
                            pix = page.get_pixmap(dpi=150)
                            image_path = f"{filepath}.png"
                            pix.save(image_path)
                    
                    base64_image = encode_image(image_path)
                    if image_path != filepath: os.remove(image_path)

                    prompt_text = "You are an expert invoice processing agent... Your entire response must be a single, valid JSON object... { \"invoice_fields\": { ... }, \"line_items\": [ ... ] }"
                    response = openai_client.chat.completions.create(
                        model="gpt-4o-mini", response_format={"type": "json_object"},
                        messages=[{"role": "user", "content": [{"type": "text", "text": prompt_text}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}]}],
                        max_tokens=2048
                    )
                    extracted_data = json.loads(response.choices[0].message.content)
                    all_fields = extracted_data.get("invoice_fields", {})
                    line_items = extracted_data.get("line_items", [])
                
                else:
                    flash("Invalid parser selected.")
                    if os.path.exists(filepath): os.remove(filepath)
                    return redirect(url_for('upload_invoice'))
                
                if AZURE_STORAGE_CONNECTION_STRING and AZURE_SHARE_NAME:
                    try:
                        file_client = ShareFileClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING, AZURE_SHARE_NAME, unique_filename)
                        with open(filepath, "rb") as source_file: file_client.upload_file(source_file)
                        logging.info("Successfully archived file to Azure File Share.")
                    except Exception as e:
                        logging.error(f"Failed to upload file to Azure File Share: {e}")
                        flash("Invoice parsed, but failed to archive.")
                
                return render_template(
                    'result.html', invoice_fields=all_fields, line_items=line_items,
                    static_file=f"uploads/{unique_filename}", orig_filename=filename,
                    result_json=json.dumps({"invoice_fields": all_fields, "line_items": line_items}, indent=2, default=str)
                )

            except Exception as e:
                logging.error(f"An unexpected error occurred: {e}")
                flash(f"An unexpected error occurred: {e}")
                if os.path.exists(filepath): os.remove(filepath)
                return redirect(url_for('upload_invoice'))
    return render_template('upload.html')

if __name__ == "__main__":
    app.run(debug=True)
