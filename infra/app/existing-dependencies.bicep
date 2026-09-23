targetScope = 'subscription'

param name string
param location string
param tags object = {}
param managedIdentityPrincipalId string
param managedIdentityClientId string
param assignExistingResourceRoles bool = false

param serviceBusResourceGroupName string
param serviceBusNamespaceName string
param serviceBusQueueName string
param dtsResourceGroupName string
param dtsSchedulerName string
param dtsTaskHubName string
param dtsEndpoint string
param logAnalyticsResourceGroupName string
param logAnalyticsWorkspaceName string
param videoStorageResourceGroupName string
param videoStorageAccountName string

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2024-01-01' existing = {
  scope: resourceGroup(serviceBusResourceGroupName)
  name: serviceBusNamespaceName
}

resource serviceBusQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  parent: serviceBusNamespace
  name: serviceBusQueueName
}

resource dtsScheduler 'Microsoft.DurableTask/schedulers@2025-04-01-preview' existing = {
  scope: resourceGroup(dtsResourceGroupName)
  name: dtsSchedulerName
}

resource dtsTaskHub 'Microsoft.DurableTask/schedulers/taskHubs@2025-04-01-preview' existing = {
  parent: dtsScheduler
  name: dtsTaskHubName
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  scope: resourceGroup(logAnalyticsResourceGroupName)
  name: logAnalyticsWorkspaceName
}

resource videoStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  scope: resourceGroup(videoStorageResourceGroupName)
  name: videoStorageAccountName
}

module serviceBusRbac './servicebus-rbac.bicep' = if (assignExistingResourceRoles) {
  name: '${name}-servicebus-rbac'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    name: name
    location: location
    tags: tags
    namespaceName: serviceBusNamespaceName
    queueName: serviceBusQueueName
    managedIdentityPrincipalId: managedIdentityPrincipalId
  }
}

module dtsRbac './dts-rbac.bicep' = if (assignExistingResourceRoles) {
  name: '${name}-dts-rbac'
  scope: resourceGroup(dtsResourceGroupName)
  params: {
    name: name
    location: location
    tags: tags
    schedulerName: dtsSchedulerName
    managedIdentityPrincipalId: managedIdentityPrincipalId
  }
}

module videoStorageRbac './video-storage-rbac.bicep' = if (assignExistingResourceRoles) {
  name: '${name}-video-storage-rbac'
  scope: resourceGroup(videoStorageResourceGroupName)
  params: {
    name: name
    storageAccountName: videoStorageAccount.name
    managedIdentityPrincipalId: managedIdentityPrincipalId
  }
}

output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id
output appSettings object = {
  AZURE_CLIENT_ID: managedIdentityClientId
  ServiceBusConnection__fullyQualifiedNamespace: '${serviceBusNamespace.name}.servicebus.windows.net'
  ServiceBusConnection__credential: 'managedidentity'
  ServiceBusConnection__clientId: managedIdentityClientId
  VIDEO_SERVICE_BUS_QUEUE_NAME: serviceBusQueue.name
  DURABLE_TASK_SCHEDULER_CONNECTION_STRING: 'Endpoint=${dtsEndpoint};TaskHub=${dtsTaskHub.name};Authentication=ManagedIdentity;ClientID=${managedIdentityClientId}'
  TASKHUB_NAME: dtsTaskHub.name
}
