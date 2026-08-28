targetScope = 'subscription'

@minLength(1)
@maxLength(64)
@description('Environment name used to derive unique resource names.')
param environmentName string

@minLength(1)
@description('Primary location for the Flex Consumption Function App.')
@allowed([
  'australiaeast'
  'australiasoutheast'
  'brazilsouth'
  'canadacentral'
  'centralindia'
  'centralus'
  'eastasia'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'italynorth'
  'japaneast'
  'koreacentral'
  'northcentralus'
  'northeurope'
  'norwayeast'
  'southafricanorth'
  'southcentralus'
  'southeastasia'
  'southindia'
  'spaincentral'
  'swedencentral'
  'uaenorth'
  'uksouth'
  'ukwest'
  'westcentralus'
  'westeurope'
  'westus'
  'westus2'
  'westus3'
])
@metadata({
  azd: {
    type: 'location'
  }
})
param location string

param apiServiceName string = ''
param apiUserAssignedIdentityName string = ''
param applicationInsightsName string = ''
param appServicePlanName string = ''
param resourceGroupName string = ''
param storageAccountName string = ''

@description('Existing-resource role assignments are disabled until separately approved.')
param assignExistingResourceRoles bool = false

param serviceBusResourceGroupName string = 'agentsdcpp'
param serviceBusNamespaceName string = 'agentvideo-namespace'
param serviceBusQueueName string = 'ltx25msrjob'
param dtsResourceGroupName string = 'agentsdcpp'
param dtsSchedulerName string = 'agentvideo'
param dtsTaskHubName string = 'default'
param dtsEndpoint string = 'https://agentvideo-atgnfafbfvdrg.westus3.durabletask.io'
param logAnalyticsResourceGroupName string = 'agentsdcpp'
param logAnalyticsWorkspaceName string = 'workspaceagentsdcpp8688'
param videoStorageResourceGroupName string = 'agentsdcpp'
param videoStorageAccountName string = 'fluxstorageaca'
param videoBlobBaseUrl string = 'https://${videoStorageAccountName}.blob.${environment().suffixes.storage}/ltxavatarjob/agentvideo'

@minValue(1)
param mcpWaitBudgetSeconds int = 20
@minValue(1)
param mcpPollIntervalSeconds int = 5
@minValue(1)
param orchestrationTimeoutSeconds int = 7200
@minValue(1)
@maxValue(86400)
param videoSasTtlSeconds int = 3600

var abbrs = loadJsonContent('./abbreviations.json')
var resourceToken = toLower(uniqueString(subscription().id, environmentName, location))
var tags = { 'azd-env-name': environmentName }
var functionAppName = !empty(apiServiceName)
  ? apiServiceName
  : '${abbrs.webSitesFunctions}video-${resourceToken}'
var deploymentStorageContainerName = 'app-package-${take(functionAppName, 32)}-${take(toLower(uniqueString(functionAppName, resourceToken)), 7)}'

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: !empty(resourceGroupName)
    ? resourceGroupName
    : '${abbrs.resourcesResourceGroups}${environmentName}'
  location: location
  tags: tags
}

module apiIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.4.1' = {
  name: 'api-user-assigned-identity'
  scope: resourceGroup
  params: {
    location: location
    tags: tags
    name: !empty(apiUserAssignedIdentityName)
      ? apiUserAssignedIdentityName
      : '${abbrs.managedIdentityUserAssignedIdentities}video-${resourceToken}'
  }
}

module appServicePlan 'br/public:avm/res/web/serverfarm:0.1.1' = {
  name: 'app-service-plan'
  scope: resourceGroup
  params: {
    name: !empty(appServicePlanName)
      ? appServicePlanName
      : '${abbrs.webServerFarms}${resourceToken}'
    sku: {
      name: 'FC1'
      tier: 'FlexConsumption'
    }
    reserved: true
    location: location
    tags: tags
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.8.3' = {
  name: 'storage'
  scope: resourceGroup
  params: {
    name: !empty(storageAccountName)
      ? storageAccountName
      : '${abbrs.storageStorageAccounts}${resourceToken}'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    dnsEndpointType: 'Standard'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containers: [
        {
          name: deploymentStorageContainerName
        }
      ]
    }
    minimumTlsVersion: 'TLS1_2'
    location: location
    tags: tags
  }
}

module existingDependencies './app/existing-dependencies.bicep' = {
  name: 'existing-dependencies'
  params: {
    name: 'existing-dependencies'
    location: location
    tags: tags
    managedIdentityPrincipalId: apiIdentity.outputs.principalId
    managedIdentityClientId: apiIdentity.outputs.clientId
    assignExistingResourceRoles: assignExistingResourceRoles
    serviceBusResourceGroupName: serviceBusResourceGroupName
    serviceBusNamespaceName: serviceBusNamespaceName
    serviceBusQueueName: serviceBusQueueName
    dtsResourceGroupName: dtsResourceGroupName
    dtsSchedulerName: dtsSchedulerName
    dtsTaskHubName: dtsTaskHubName
    dtsEndpoint: dtsEndpoint
    logAnalyticsResourceGroupName: logAnalyticsResourceGroupName
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    videoStorageResourceGroupName: videoStorageResourceGroupName
    videoStorageAccountName: videoStorageAccountName
  }
}

module monitoring 'br/public:avm/res/insights/component:0.6.0' = {
  name: 'application-insights'
  scope: resourceGroup
  params: {
    name: !empty(applicationInsightsName)
      ? applicationInsightsName
      : '${abbrs.insightsComponents}${resourceToken}'
    location: location
    tags: tags
    workspaceResourceId: existingDependencies.outputs.logAnalyticsWorkspaceId
    disableLocalAuth: true
  }
}

var storageEndpointConfig = {
  enableBlob: true
  enableQueue: true
  enableTable: false
}

module api './app/api.bicep' = {
  name: 'api'
  scope: resourceGroup
  params: {
    name: functionAppName
    location: location
    tags: tags
    applicationInsightsName: monitoring.outputs.name
    appServicePlanId: appServicePlan.outputs.resourceId
    runtimeName: 'python'
    runtimeVersion: '3.13'
    storageAccountName: storage.outputs.name
    enableBlob: storageEndpointConfig.enableBlob
    enableQueue: storageEndpointConfig.enableQueue
    enableTable: storageEndpointConfig.enableTable
    deploymentStorageContainerName: deploymentStorageContainerName
    identityId: apiIdentity.outputs.resourceId
    identityClientId: apiIdentity.outputs.clientId
    maximumInstanceCount: 10
    instanceMemoryMB: 2048
    appSettings: union(existingDependencies.outputs.appSettings, {
      MCP_WAIT_BUDGET_SECONDS: '${mcpWaitBudgetSeconds}'
      MCP_POLL_INTERVAL_SECONDS: '${mcpPollIntervalSeconds}'
      ORCHESTRATION_TIMEOUT_SECONDS: '${orchestrationTimeoutSeconds}'
      VIDEO_BLOB_BASE_URL: videoBlobBaseUrl
      VIDEO_SAS_TTL_SECONDS: '${videoSasTtlSeconds}'
    })
  }
}

module rbac './app/rbac.bicep' = {
  name: 'rbac-assignments'
  scope: resourceGroup
  params: {
    name: 'rbac'
    location: location
    tags: tags
    storageAccountName: storage.outputs.name
    appInsightsName: monitoring.outputs.name
    managedIdentityPrincipalId: apiIdentity.outputs.principalId
    enableBlob: storageEndpointConfig.enableBlob
    enableQueue: storageEndpointConfig.enableQueue
    enableTable: storageEndpointConfig.enableTable
  }
}

output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = resourceGroup.name
output SERVICE_API_NAME string = api.outputs.SERVICE_API_NAME
output AZURE_FUNCTION_NAME string = api.outputs.SERVICE_API_NAME
