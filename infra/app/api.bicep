param name string
@description('Primary location for the Flex Consumption Function App.')
param location string = resourceGroup().location
param tags object = {}
param applicationInsightsName string
param appServicePlanId string
param appSettings object = {}
param runtimeName string
param runtimeVersion string
param serviceName string = 'api'
param storageAccountName string
param deploymentStorageContainerName string
param instanceMemoryMB int = 2048
param maximumInstanceCount int = 10
param identityId string
param identityClientId string
param enableBlob bool = true
param enableQueue bool = true
param enableTable bool = false

var applicationInsightsIdentity = 'ClientId=${identityClientId};Authorization=AAD'
var baseAppSettings = {
  AzureWebJobsStorage__credential: 'managedidentity'
  AzureWebJobsStorage__clientId: identityClientId
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: applicationInsightsIdentity
  APPLICATIONINSIGHTS_CONNECTION_STRING: applicationInsights.properties.ConnectionString
}
var blobSettings = enableBlob ? {
  AzureWebJobsStorage__blobServiceUri: storage.properties.primaryEndpoints.blob
} : {}
var queueSettings = enableQueue ? {
  AzureWebJobsStorage__queueServiceUri: storage.properties.primaryEndpoints.queue
} : {}
var tableSettings = enableTable ? {
  AzureWebJobsStorage__tableServiceUri: storage.properties.primaryEndpoints.table
} : {}
var allAppSettings = union(
  appSettings,
  blobSettings,
  queueSettings,
  tableSettings,
  baseAppSettings
)

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

module api 'br/public:avm/res/web/site:0.15.1' = {
  name: '${serviceName}-flex-consumption'
  params: {
    kind: 'functionapp,linux'
    name: name
    location: location
    tags: union(tags, { 'azd-service-name': serviceName })
    serverFarmResourceId: appServicePlanId
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [
        identityId
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentStorageContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identityId
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
      }
      runtime: {
        name: runtimeName
        version: runtimeVersion
      }
    }
    siteConfig: {
      alwaysOn: false
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
    }
    appSettingsKeyValuePairs: allAppSettings
    httpsOnly: true
  }
}

output SERVICE_API_NAME string = api.outputs.name

