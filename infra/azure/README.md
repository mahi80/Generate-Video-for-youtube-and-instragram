# Deploy to Azure (Container Apps + PostgreSQL)

This deploys the SaaS version (Google sign-in, per-user videos, admin dashboard) as:

| Azure resource | Purpose |
|---|---|
| Container Apps (1 replica, 2 vCPU / 4 GB) | The web app and video rendering |
| Azure Database for PostgreSQL Flexible Server (B1ms) | Users, sign-ins and video jobs |
| Azure Files share mounted at `/data` | `config.toml`, generated videos and caches |
| Container Registry (Basic) | Your app image |
| Log Analytics | App logs |

Rough cost: **$45–90/month** at low traffic (PostgreSQL B1ms ≈ $13, Container Apps with one always-on 2 vCPU replica ≈ $30–70, Registry ≈ $5, storage and logs a few dollars). Check the Azure pricing calculator for your region.

> **One replica only.** Video tasks, sessions and settings live in the app's memory, so the template pins the app to exactly one replica. Scaling out requires moving the task queue to a shared service first.

---

## 1. Prerequisites

1. An Azure subscription.
2. The Azure CLI, installed in WSL:
   ```bash
   curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
   az login
   az bicep install
   ```
3. Check the template compiles:
   ```bash
   az bicep build --file infra/azure/main.bicep --stdout > /dev/null && echo OK
   ```

## 2. Create the resources

```bash
RG=mpt-prod
LOCATION=centralindia            # or eastus, westeurope, ...
az group create -n $RG -l $LOCATION

az deployment group create -g $RG \
  --template-file infra/azure/main.bicep \
  --parameters prefix=mpt \
               adminEmails="you@gmail.com" \
               postgresAdminPassword="$(openssl rand -base64 24)" \
               sessionSecret="$(openssl rand -hex 32)" \
  --query properties.outputs
```

Note the outputs: `appUrl`, `googleRedirectUri` and `registryLoginServer`.

> Save the PostgreSQL password if you will ever connect to the database yourself. The app gets its connection string from a Container Apps secret.

## 3. Build and push the image

The build runs in Azure, so you don't need Docker for this step:

```bash
ACR=$(az acr list -g $RG --query "[0].name" -o tsv)
az acr build -r $ACR -t moneyprinterturbo:v1 \
  --build-arg DOCKER_BUILD_MIRROR=default --build-arg PIP_USE_OFFICIAL=1 .
```

Deploy again with your image. Repeat the same parameters, plus `containerImage`:

```bash
az deployment group create -g $RG --template-file infra/azure/main.bicep \
  --parameters prefix=mpt adminEmails="you@gmail.com" \
               postgresAdminPassword="<same as before>" sessionSecret="<same as before>" \
               containerImage="$ACR.azurecr.io/moneyprinterturbo:v1"
```

## 4. Set up Google sign-in

1. Open <https://console.cloud.google.com/> and create a project, or pick an existing one.
2. **APIs & Services → OAuth consent screen**: choose **External**, fill in the app name and support email, and add your domain. While it is in *Testing*, add your testers' Google accounts. Publish it when you go live.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: the `googleRedirectUri` output, e.g. `https://mpt-web.<something>.azurecontainerapps.io/auth/google/callback`
   - For local testing, also add `http://localhost:8510/auth/google/callback`
4. Copy the **Client ID** and **Client secret** and deploy again with:
   ```bash
   --parameters googleClientId="<id>.apps.googleusercontent.com" googleClientSecret="<secret>"
   ```

## 5. First sign-in and platform settings

1. Open `appUrl` and click **Continue with Google**. Emails listed in `adminEmails` become admins.
2. Go to **Studio → Settings** and set the platform keys:
   - **LLM:** Ollama does not run on Container Apps (there's no GPU). Pick a hosted provider such as OpenRouter, DeepSeek, Gemini or Azure OpenAI.
   - **Material sources:** your Pixabay key, with Pixabay selected as the video source.
   - **Auto-publish:** Upload-Post, if you use it.

   These settings are saved to `config.toml` on the Azure Files share, so they survive restarts and redeploys.
3. Regular users only see **Create** and **My videos**. They never see your keys.

## Updating

```bash
az acr build -r $ACR -t moneyprinterturbo:v2 --build-arg DOCKER_BUILD_MIRROR=default --build-arg PIP_USE_OFFICIAL=1 .
az containerapp update -g $RG -n mpt-web --image $ACR.azurecr.io/moneyprinterturbo:v2
```

Database migrations run automatically when the app starts.

## Custom domain

```bash
az containerapp hostname add -g $RG -n mpt-web --hostname app.yourdomain.com
az containerapp hostname bind -g $RG -n mpt-web --hostname app.yourdomain.com --environment mpt-env --validation-method CNAME
```

Then set `PUBLIC_BASE_URL` to `https://app.yourdomain.com`, and add `https://app.yourdomain.com/auth/google/callback` to the Google OAuth client:

```bash
az containerapp update -g $RG -n mpt-web --set-env-vars PUBLIC_BASE_URL=https://app.yourdomain.com
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `redirect_uri_mismatch` from Google | The redirect URI in Google Cloud must exactly match `googleRedirectUri`, including `https` and the path |
| App restarts or logs show `could not connect to server` | Check the PostgreSQL firewall rule `AllowAzureServices` and that the server is running |
| Videos fail at the script step | Set a hosted LLM provider in Studio → Settings |
| Logs | `az containerapp logs show -g $RG -n mpt-web --follow` |
