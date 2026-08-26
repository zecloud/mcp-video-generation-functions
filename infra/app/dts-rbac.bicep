param name string
// The shared module contract keeps location/tags available for future diagnostics.
#disable-next-line no-unused-params
param location string = resourceGroup().location
#disable-next-line no-unused-params
param tags object = {}
param schedulerName string
param managedIdentityPrincipalId string

var durableTaskDataContributorRoleId = '0ad04412-c4d5-4796-b79c-f76d14c8d402'

resource dtsScheduler 'Microsoft.DurableTask/schedulers@2025-04-01-preview' existing = {
  name: schedulerName
}

resource dtsContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dtsScheduler.id, managedIdentityPrincipalId, durableTaskDataContributorRoleId, name)
  scope: dtsScheduler
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      durableTaskDataContributorRoleId
    )
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

