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

# --- Azure Clients (remains the same) ---
document_intelligence_client = DocumentIntelligenceClient(
    endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY)
)

# --- Helper Functions (remains the same) ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def safe_val(v):
    if v is None: return None
    if hasattr(v, "value_address") and v.value_address is not None:
        addr = v.value_address
        parts = [getattr(addr, f) for f in ["house_number", "road", "city", "state", "postal_code", "country_region"] if getattr(addr, f, None)]
        return ", ".join(parts)
    if hasattr(v, "value_currency") and v.value_currency is not None:
        cur = v.value_currency
        amount = getattr(cur, "amount", None)
        symbol = getattr(cur, "symbol", "")
        return f"{symbol}{amount}" if amount is not None else symbol
    for attr in ["value_string", "value_number", "value_date", "value"]:
        if hasattr(v, attr):
            val = getattr(v, attr)
            if val is not None: return str(val)
    return str(v)

# --- Routes ---
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
                
                # 1. Analyze with Document Intelligence
                with open(filepath, "rb") as f:
                    poller = document_intelligence_client.begin_analyze_document("prebuilt-invoice", f)
                    result = poller.result()
                
                invoice_data = {}
                if result.documents:
                    # *** THIS IS THE LOGIC THAT WAS MISSING AND IS NOW RESTORED ***
                    doc = result.documents[0]
                    fields = doc.fields
                    invoice_data['InvoiceId'] = safe_val(fields.get("InvoiceId"))
                    invoice_data['InvoiceDate'] = safe_val(fields.get("InvoiceDate"))
                    invoice_data['PurchaseOrder'] = safe_val(fields.get("PurchaseOrder"))
                    invoice_data['VendorName'] = safe_val(fields.get("VendorName"))
                    invoice_data['CustomerName'] = safe_val(fields.get("CustomerName"))
                    invoice_data['BillingAddress'] = safe_val(fields.get("BillingAddress"))
                    invoice_data['ShippingAddress'] = safe_val(fields.get("ShippingAddress"))
                    invoice_data['SubTotal'] = safe_val(fields.get("SubTotal"))
                    invoice_data['TotalTax'] = safe_val(fields.get("TotalTax"))
                    invoice_data['AmountDue'] = safe_val(fields.get("AmountDue"))

                    items = []
                    if fields.get("Items"):
                        for item in fields.get("Items").value_array:
                            item_fields = item.value_object
                            item_val = lambda key: safe_val(item_fields.get(key))
                            items.append({
                                "Description": item_val("Description"), "Quantity": item_val("Quantity"),
                                "Unit": item_val("Unit"), "UnitPrice": item_val("UnitPrice"),
                                "ProductCode": item_val("ProductCode"), "Tax": item_val("Tax"),
                                "Amount": item_val("Amount"),
                            })
                    invoice_data['Items'] = items
                    # *** END OF RESTORED LOGIC ***
                else:
                    flash("No documents were found in the provided file.")
                    if os.path.exists(filepath): os.remove(filepath)
                    return redirect(url_for('upload_invoice'))

                # 2. Upload to Azure File Share
                try:
                    logging.info(f"Uploading '{unique_filename}' to Azure File Share '{AZURE_SHARE_NAME}'...")
                    file_client = ShareFileClient.from_connection_string(
                        conn_str=AZURE_STORAGE_CONNECTION_STRING,
                        share_name=AZURE_SHARE_NAME,
                        file_path=unique_filename
                    )
                    with open(filepath, "rb") as source_file:
                        file_client.upload_file(source_file)
                    logging.info("Upload to Azure File Share successful.")
                except Exception as e:
                    logging.error(f"Failed to upload file to Azure File Share: {e}")
                    flash("Invoice was parsed, but failed to archive. Please contact support.")

                # 3. Render the result
                result_json = invoice_data
                static_file = f"uploads/{unique_filename}"
                return render_template(
                    'result.html',
                    invoice_data=invoice_data,
                    static_file=static_file,
                    orig_filename=filename,
                    result_json=json.dumps(result_json, indent=2)
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