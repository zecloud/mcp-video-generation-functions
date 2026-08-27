param name string
// The shared module contract keeps location/tags available for future diagnostics.
#disable-next-line no-unused-params
param location string = resourceGroup().location
#disable-next-line no-unused-params
param tags object = {}
param namespaceName string
param queueName string
param managedIdentityPrincipalId string

var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2024-01-01' existing = {
  name: namespaceName
}

resource serviceBusQueue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  parent: serviceBusNamespace
  name: queueName
}

resource serviceBusSenderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBusQueue.id, managedIdentityPrincipalId, serviceBusDataSenderRoleId, name)
  scope: serviceBusQueue
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      serviceBusDataSenderRoleId
    )
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

