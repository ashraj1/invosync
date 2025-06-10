import os
import uuid
import json
import logging
from flask import Flask, request, render_template, flash, redirect, url_for
from werkzeug.utils import secure_filename
from azure.core.exceptions import HttpResponseError
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.storage.fileshare import ShareFileClient

# --- Configuration (remains the same) ---
AZURE_ENDPOINT = os.environ.get("AZURE_ENDPOINT")
AZURE_KEY = os.environ.get("AZURE_KEY")
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
AZURE_SHARE_NAME = os.environ.get("AZURE_SHARE_NAME")

if not AZURE_ENDPOINT or not AZURE_KEY:
    raise ValueError("Azure Endpoint and Key must be set as environment variables.")
if not AZURE_STORAGE_CONNECTION_STRING or not AZURE_SHARE_NAME:
    raise ValueError("Azure Storage Connection String and Share Name must be set as environment variables.")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("FLASK_SECRET_KEY", "a-strong-default-secret-key")
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'pdf'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
logging.basicConfig(level=logging.INFO)

document_intelligence_client = DocumentIntelligenceClient(
    endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY)
)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

# --- MODIFIED SECTION: Enhanced safe_val function ---
def safe_val(v):
    """
    Recursively and safely extracts and formats values from Azure's response objects,
    handling simple and complex nested types.
    """
    if v is None:
        return None

    # Check for specific value types first and extract the readable value
    if hasattr(v, "value_address") and v.value_address:
        addr = v.value_address
        parts = [getattr(addr, f) for f in ["house_number", "road", "city", "state", "postal_code", "country_region"] if getattr(addr, f, None)]
        return ", ".join(parts)
    
    if hasattr(v, "value_currency") and v.value_currency:
        cur = v.value_currency
        amount = getattr(cur, "amount", None)
        symbol = getattr(cur, "symbol", "") or getattr(cur, "currency_code", "")
        return f"{symbol}{amount}" if amount is not None else symbol

    if hasattr(v, "value_phone_number") and v.value_phone_number:
        return v.value_phone_number

    if hasattr(v, "value_array") and v.value_array:
        # Recursively process each item in the array and join them into a readable string
        return ", ".join([safe_val(item) for item in v.value_array])

    if hasattr(v, "value_object") and v.value_object:
        # Recursively process each key-value pair in the object
        parts = []
        for key, value in v.value_object.items():
            formatted_value = safe_val(value)
            if formatted_value:  # Only include if there is a value
                parts.append(f"{key}: {formatted_value}")
        return "; ".join(parts)

    # Fallback for simple types
    for attr in ["value_string", "value_number", "value_date"]:
        if hasattr(v, attr):
            val = getattr(v, attr)
            if val is not None:
                return str(val)

    # If no specific value_... attribute is found, use the raw content
    if hasattr(v, "content"):
        return v.content
    
    # Final fallback if the object is something else entirely
    return str(v)
# --- END OF MODIFIED SECTION ---


@app.route('/', methods=['GET', 'POST'])
def upload_invoice():
    if request.method == 'POST':
        if 'invoice' not in request.files:
            flash('No file part provided.')
            return redirect(request.url)
        file = request.files['invoice']
        if file.filename == '':
            flash('No file selected.')
            return redirect(request.url)

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            unique_filename = f"{uuid.uuid4().hex}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            
            try:
                file.save(filepath)
                
                with open(filepath, "rb") as f:
                    poller = document_intelligence_client.begin_analyze_document("prebuilt-invoice", f)
                    result = poller.result()
                
                all_fields = {}
                line_items = []
                if result.documents:
                    doc = result.documents[0]
                    for key, field in doc.fields.items():
                        if key == "Items":
                            if field.value_array:
                                for item in field.value_array:
                                    item_fields = item.value_object
                                    item_val = lambda k: safe_val(item_fields.get(k))
                                    line_items.append({
                                        "Description": item_val("Description"), "Quantity": item_val("Quantity"),
                                        "Unit": item_val("Unit"), "UnitPrice": item_val("UnitPrice"),
                                        "ProductCode": item_val("ProductCode"), "Tax": item_val("Tax"),
                                        "Amount": item_val("Amount"),
                                    })
                        else:
                            all_fields[key] = safe_val(field)
                else:
                    flash("No documents were found in the provided file.")
                    if os.path.exists(filepath): os.remove(filepath)
                    return redirect(url_for('upload_invoice'))

                try:
                    logging.info(f"Uploading '{unique_filename}' to Azure File Share '{AZURE_SHARE_NAME}'...")
                    file_client = ShareFileClient.from_connection_string(conn_str=AZURE_STORAGE_CONNECTION_STRING, share_name=AZURE_SHARE_NAME, file_path=unique_filename)
                    with open(filepath, "rb") as source_file:
                        file_client.upload_file(source_file)
                    logging.info("Upload to Azure File Share successful.")
                except Exception as e:
                    logging.error(f"Failed to upload file to Azure File Share: {e}")
                    flash("Invoice was parsed, but failed to archive. Please contact support.")

                result_json = all_fields.copy()
                result_json["Items"] = line_items
                static_file = f"uploads/{unique_filename}"
                return render_template(
                    'result.html',
                    invoice_fields=all_fields,
                    line_items=line_items,
                    static_file=static_file,
                    orig_filename=filename,
                    result_json=json.dumps(result_json, indent=2, default=str)
                )
            except Exception as e:
                logging.error(f"An unexpected error occurred during processing: {e}")
                flash("An unexpected server error occurred. Please try again.")
                if os.path.exists(filepath): os.remove(filepath)
                return redirect(url_for('upload_invoice'))
        else:
            flash(f"Invalid file type. Allowed types are: {', '.join(app.config['ALLOWED_EXTENSIONS'])}")
            return redirect(request.url)
    return render_template('upload.html')

if __name__ == "__main__":
    app.run(debug=True)