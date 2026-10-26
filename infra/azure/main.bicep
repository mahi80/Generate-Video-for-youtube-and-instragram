// MoneyPrinterTurbo SaaS on Azure Container Apps.
//
// Creates: Log Analytics, Container Registry, Container Apps environment,
// Azure Files share (config.toml + generated videos), PostgreSQL Flexible
// Server, a user-assigned identity that can pull from the registry, and the
// web app itself. See README.md in this folder for the step-by-step guide.

@description('Short lowercase prefix used in resource names, e.g. "mpt".')
@minLength(3)
@maxLength(12)
param prefix string = 'mpt'

@description('Azure region. Pick one close to your users.')
param location string = resourceGroup().location

@description('Container image to run, e.g. <registry>.azurecr.io/moneyprinterturbo:v1. Leave the default for the first deployment, then deploy again with your image.')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('PostgreSQL administrator login name.')
param postgresAdminUser string = 'mptadmin'

@description('PostgreSQL administrator password.')
@secure()
param postgresAdminPassword string

@description('Random secret used to sign login sessions (at least 32 characters).')
@secure()
param sessionSecret string

@description('Google OAuth client ID (from Google Cloud Console). Can be added later.')
param googleClientId string = ''

@description('Google OAuth client secret. Can be added later.')
@secure()
param googleClientSecret string = ''

@description('Comma-separated emails that become admins, e.g. "you@gmail.com".')
param adminEmails string

@description('vCPU for the app. Video rendering is CPU heavy; 2 or 4 is recommended.')
param cpu string = '2.0'

@description('Memory for the app; must match the CPU tier (2.0 -> 4Gi, 4.0 -> 8Gi).')
param memory string = '4Gi'

var suffix = uniqueString(resourceGroup().id, prefix)
var appName = '${prefix}-web'
var registryName = toLower(replace('${prefix}acr${suffix}', '-', ''))
var storageName = toLower(take(replace('${prefix}st${suffix}', '-', ''), 24))
var postgresName = '${prefix}-pg-${suffix}'
var shareName = 'mpt-data'
var databaseName = 'mpt'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-identity'
  location: location
}

// AcrPull, so the app can pull images without registry passwords.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, 'acrpull')
  scope: registry
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: { shareQuota: 100 }
}

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: postgresName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminUser
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: 32 }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgres
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// 0.0.0.0 - 0.0.0.0 allows connections from Azure services (the app), not the internet.
resource allowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: postgres
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource environmentStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: environment
  name: 'mptdata'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: share.name
      accessMode: 'ReadWrite'
    }
  }
}

var publicUrl = 'https://${appName}.${environment.properties.defaultDomain}'
var databaseUrl = 'postgresql+psycopg://${postgresAdminUser}:${uriComponent(postgresAdminPassword)}@${postgres.properties.fullyQualifiedDomainName}:5432/${databaseName}?sslmode=require'

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [ acrPull, environmentStorage, database, allowAzure ]
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8510
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registry.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        { name: 'database-url', value: databaseUrl }
        { name: 'session-secret', value: sessionSecret }
        { name: 'google-client-secret', value: empty(googleClientSecret) ? 'unset' : googleClientSecret }
      ]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: containerImage
          command: [ 'python', '-m', 'webui_htmx', '--host', '0.0.0.0', '--port', '8510' ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: [
            { name: 'DATABASE_URL', secretRef: 'database-url' }
            { name: 'SESSION_SECRET', secretRef: 'session-secret' }
            { name: 'GOOGLE_CLIENT_ID', value: googleClientId }
            { name: 'GOOGLE_CLIENT_SECRET', secretRef: 'google-client-secret' }
            { name: 'ADMIN_EMAILS', value: adminEmails }
            { name: 'PUBLIC_BASE_URL', value: publicUrl }
            { name: 'SECURE_COOKIES', value: '1' }
            // config.toml, generated videos and caches live on the Azure Files share.
            { name: 'MPT_HOME', value: '/data' }
          ]
          volumeMounts: [
            { volumeName: 'data', mountPath: '/data' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8510 }
              initialDelaySeconds: 30
              periodSeconds: 30
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: environmentStorage.name
        }
      ]
      // Tasks, sessions and settings live in one process: exactly one replica.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output appUrl string = publicUrl
output googleRedirectUri string = '${publicUrl}/auth/google/callback'
output registryLoginServer string = registry.properties.loginServer
output postgresHost string = postgres.properties.fullyQualifiedDomainName
