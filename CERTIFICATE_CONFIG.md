# Certificate Configuration Examples

This document shows how to configure Azure AD certificate authentication with separate certificate and private key variables.

## Quick Start: Separate Certificate and Key

### 1. Generate Certificate and Key

```bash
# Generate certificate and private key
openssl req -x509 -newkey rsa:4096 -keyout private_key.pem -out certificate.pem -days 365 -nodes -subj "/CN=Intune2Snipe"

# Upload certificate.pem to Azure AD App Registration
# Keep private_key.pem secure
```

### 2. Choose a Secret Storage Method

Pick **one** of the following options for your environment:

---

#### Option A: .env File (Local Development)

No extra dependencies needed.

```bash
# Copy the example and edit with your values
cp .env.example .env
```

Add to your `.env`:
```bash
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CERTIFICATE_PEM="$(cat certificate.pem)"
AZURE_PRIVATE_KEY_PEM="$(cat private_key.pem)"
SNIPEIT_URL=https://your-snipeit.com/api/v1
SNIPEIT_API_TOKEN=your-token
SNIPEIT_DEFAULT_STATUS="Ready to Deploy"
```

Run:
```bash
python3 app.py
```

---

#### Option B: AWS Secrets Manager

1. Uncomment `boto3` in `requirements.txt` and reinstall:
   ```bash
   # In requirements.txt, uncomment:
   # boto3>=1.28.0,<2.0.0

   pip install -r requirements.txt
   ```

2. Store the secret:
   ```bash
   aws secretsmanager create-secret \
     --name intune2snipe/prod \
     --secret-string "{
       \"AZURE_TENANT_ID\": \"your-tenant-id\",
       \"AZURE_CLIENT_ID\": \"your-client-id\",
       \"AZURE_CERTIFICATE_PEM\": \"$(cat certificate.pem | sed 's/$/\\n/g' | tr -d '\n')\",
       \"AZURE_PRIVATE_KEY_PEM\": \"$(cat private_key.pem | sed 's/$/\\n/g' | tr -d '\n')\",
       \"SNIPEIT_URL\": \"https://your-snipeit.com/api/v1\",
       \"SNIPEIT_API_TOKEN\": \"your-token\",
       \"SNIPEIT_DEFAULT_STATUS\": \"Ready to Deploy\"
     }"
   ```

3. Run:
   ```bash
   python3 app.py --secret-store aws-secrets-manager --secret-name intune2snipe/prod
   ```

---

#### Option C: Azure Key Vault

1. Uncomment `azure-keyvault-secrets` in `requirements.txt` and reinstall:
   ```bash
   # In requirements.txt, uncomment:
   # azure-keyvault-secrets>=4.7.0,<5.0.0

   pip install -r requirements.txt
   ```

2. Store the secrets:
   ```bash
   az keyvault secret set --vault-name your-vault --name AZURE-TENANT-ID --value "your-tenant-id"
   az keyvault secret set --vault-name your-vault --name AZURE-CLIENT-ID --value "your-client-id"
   az keyvault secret set --vault-name your-vault --name AZURE-CERTIFICATE-PEM --file certificate.pem
   az keyvault secret set --vault-name your-vault --name AZURE-PRIVATE-KEY-PEM --file private_key.pem
   az keyvault secret set --vault-name your-vault --name SNIPEIT-URL --value "https://your-snipeit.com/api/v1"
   az keyvault secret set --vault-name your-vault --name SNIPEIT-API-TOKEN --value "your-token"
   ```

3. Run:
   ```bash
   python3 app.py --secret-store azure-keyvault --keyvault-url https://your-vault.vault.azure.net/
   ```

---

#### Option D: HashiCorp Vault

1. Uncomment `hvac` in `requirements.txt` and reinstall:
   ```bash
   # In requirements.txt, uncomment:
   # hvac>=1.2.0,<3.0.0

   pip install -r requirements.txt
   ```

2. Store the secrets:
   ```bash
   vault kv put secret/intune2snipe \
     AZURE_TENANT_ID="your-tenant-id" \
     AZURE_CLIENT_ID="your-client-id" \
     AZURE_CERTIFICATE_PEM="$(cat certificate.pem)" \
     AZURE_PRIVATE_KEY_PEM="$(cat private_key.pem)" \
     SNIPEIT_URL="https://your-snipeit.com/api/v1" \
     SNIPEIT_API_TOKEN="your-token" \
     SNIPEIT_DEFAULT_STATUS="Ready to Deploy"
   ```

3. Run:
   ```bash
   export VAULT_TOKEN=your-token
   export VAULT_ADDR=https://vault.example.com:8200
   python3 app.py --secret-store vault --vault-path secret/intune2snipe
   ```

## How It Works

The script automatically detects which variables are set:

1. **Separate variables** (`AZURE_CERTIFICATE_PEM` + `AZURE_PRIVATE_KEY_PEM`):
   - Script combines them at runtime with a newline separator
   - Easier to manage in secret stores
   - Better for certificate rotation

2. **Combined variable** (`AZURE_COMBINED_CERT_KEY`):
   - Single variable with both certificate and key
   - Simpler for local `.env` files

3. **Priority**: If both methods are set, separate variables take precedence and are combined.

## Benefits of Separate Storage

- **Easier rotation**: Update certificate or key independently
- **Better access control**: Different permissions for cert vs key
- **Smaller secret values**: Some secret stores have size limits
- **Clearer audit trails**: Track certificate and key changes separately
