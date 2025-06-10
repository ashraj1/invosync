import os
import uuid
import json
from flask import Flask, request, render_template
from werkzeug.utils import secure_filename
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient

AZURE_ENDPOINT = "https://invosync360.cognitiveservices.azure.com/"
AZURE_KEY = "F6AcUxFizRwSN6RqAYg1EOubULYYuD1GZ6E8m1wbgAESU2znnAvKJQQJ99BFACYeBjFXJ3w3AAALACOGT8WG"

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'static/uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

document_intelligence_client = DocumentIntelligenceClient(
    endpoint=AZURE_ENDPOINT, credential=AzureKeyCredential(AZURE_KEY)
)

def safe_val(v):
    if v is None:
        return None
    # Address object
    if hasattr(v, "value_address") and v.value_address is not None:
        addr = v.value_address
        parts = []
        if getattr(addr, "house_number", None): parts.append(addr.house_number)
        if getattr(addr, "road", None): parts.append(addr.road)
        if getattr(addr, "city", None): parts.append(addr.city)
        if getattr(addr, "state", None): parts.append(addr.state)
        if getattr(addr, "postal_code", None): parts.append(addr.postal_code)
        if getattr(addr, "country_region", None): parts.append(addr.country_region)
        return ", ".join(parts)
    # Currency object (v4+)
    if hasattr(v, "value_currency") and v.value_currency is not None:
        cur = v.value_currency
        amount = getattr(cur, "amount", None)
        currency = getattr(cur, "currency", None)
        if amount is not None and currency:
            return f"{amount} {currency}"
        elif amount is not None:
            return str(amount)
        elif currency:
            return currency
        else:
            return None
    # Handle string, number, date
    for attr in ["value_string", "value_number", "value_date", "value"]:
        if hasattr(v, attr):
            val = getattr(v, attr)
            if val is not None:
                return val
    return str(v)

@app.route('/', methods=['GET', 'POST'])
def upload_invoice():
    if request.method == 'POST':
        file = request.files['invoice']
        if file:
            filename = secure_filename(file.filename)
            filename = f"{uuid.uuid4().hex}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            with open(filepath, "rb") as f:
                poller = document_intelligence_client.begin_analyze_document(
                    "prebuilt-invoice", f
                )
                result = poller.result()
                if hasattr(result, "to_json"):
                    result_json = json.loads(result.to_json())
                else:
                    result_json = {}

            invoice_data = {}
            if result.documents:
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

                # Line items
                items = []
                if fields.get("Items"):
                    for item in fields["Items"].value_array:
                        item_fields = item.value_object
                        def item_val(key):
                            v = item_fields.get(key)
                            return safe_val(v)
                        items.append({
                            "Description": item_val("Description"),
                            "Quantity": item_val("Quantity"),
                            "Unit": item_val("Unit"),
                            "UnitPrice": item_val("UnitPrice"),
                            "ProductCode": item_val("ProductCode"),
                            "Tax": item_val("Tax"),
                            "Amount": item_val("Amount"),
                        })
                invoice_data['Items'] = items
            else:
                print("No documents found in Azure result!")
                invoice_data['Items'] = []

            # JSON output for debugging/viewing
            result_json = result.to_dict() if hasattr(result, "to_dict") else {}
            static_file = f"uploads/{filename}"
            return render_template(
                'result.html',
                invoice_data=invoice_data,
                static_file=static_file,
                orig_filename=filename,
                result_json=json.dumps(result_json, indent=2)
)
    return render_template('upload.html')

if __name__ == "__main__":
    app.run(debug=True)
