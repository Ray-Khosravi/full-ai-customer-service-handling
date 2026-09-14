// Monitoring for the Nexroza calendar MCP server.
// Deploy at resource-group scope:
//   az deployment group create -g rg-nexroza-calendar-mcp -f infra/monitoring.bicep \
//     -p alertEmail=<owner email> -p appInsightsName=appi-<token>
//
// Cost (2026 list prices, approximate): 4 log-search alert rules at 30-min
// frequency (~US$0.50/month each) + email action group (free). About
// US$2/month.

targetScope = 'resourceGroup'

@description('Email address that receives alerts')
param alertEmail string

param appInsightsName string  // e.g. appi-<token> (azd output)
param location string = resourceGroup().location

resource appInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-nexroza-calendar'
  location: 'global'
  properties: {
    groupShortName: 'nexrozacal'
    enabled: true
    emailReceivers: [
      {
        name: 'owner'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// 1) Outlook authorization / Graph failures: heartbeat failures, silent
//    refresh failures, Graph errors. Sev1 because the chatbot is blind.
resource outlookAuthAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'nexroza-outlook-authorization-failure'
  location: location
  properties: {
    displayName: 'Nexroza: Outlook authorization or Graph failure'
    description: 'The MCP server could not use the stored Outlook authorization (re-run tools/authorize_outlook.py) or Microsoft Graph returned errors.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT30M'
    windowSize: 'PT1H'
    scopes: [appInsights.id]
    targetResourceTypes: ['Microsoft.Insights/components']
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "OUTLOOK_HEARTBEAT_FAILED" or message has "Silent token acquisition failed" or message has "Graph request failed" or message has "Microsoft Graph rejected"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// 2) MCP server errors: unexpected exceptions in tools, blob storage access
//    failures, missing technician calendars, worker crashes.
resource mcpErrorsAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'nexroza-mcp-server-errors'
  location: location
  properties: {
    displayName: 'Nexroza: MCP server errors'
    description: 'Unexpected errors in the calendar MCP tools, blob token-cache access, or degraded calendars.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT30M'
    windowSize: 'PT1H'
    scopes: [appInsights.id]
    targetResourceTypes: ['Microsoft.Insights/components']
    criteria: {
      allOf: [
        {
          query: 'union traces, exceptions | where message has "Unexpected failure correlation_id" or message has "Status check failed" or message has "Blob read failed" or message has "Blob write failed" or message startswith "OUTLOOK_HEARTBEAT_DEGRADED" or (itemType == "exception" and outerMessage has "python exited")'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// 3) HTTP 5xx responses from the Function host (covers failures the app
//    code never sees). Flex Consumption apps expose no Http5xx platform metric,
//    so this reads Application Insights requests instead.
resource http5xxAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'nexroza-function-http-5xx'
  location: location
  properties: {
    displayName: 'Nexroza: Function App HTTP 5xx'
    description: 'The calendar MCP Function App returned HTTP 5xx responses.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT30M'
    windowSize: 'PT1H'
    scopes: [appInsights.id]
    targetResourceTypes: ['Microsoft.Insights/components']
    criteria: {
      allOf: [
        {
          query: 'requests | where toint(resultCode) >= 500'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// 4) Service-request workflow failures: request failed, SMS delivery failure,
//    webhook signature failures (possible probing), poison messages,
//    technician/customer timeouts, booking failures. No customer data in the
//    query output - only markers and redacted identifiers are logged.
resource workflowAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'nexroza-workflow-failures'
  location: location
  properties: {
    displayName: 'Nexroza: service-request workflow failures'
    description: 'SERVICE_REQUEST_FAILED / SMS_SEND_FAILED / SMS_DELIVERY_FAILURE / SMS_WEBHOOK_SIGNATURE_INVALID / POISON_MESSAGE / REQUEST_EXPIRED / BOOKING_FAILED markers in the last hour.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT30M'
    windowSize: 'PT1H'
    scopes: [appInsights.id]
    targetResourceTypes: ['Microsoft.Insights/components']
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "SERVICE_REQUEST_FAILED" or message startswith "SMS_SEND_FAILED" or message startswith "SMS_DELIVERY_FAILURE" or message startswith "SMS_WEBHOOK_SIGNATURE_INVALID" or message startswith "POISON_MESSAGE" or message startswith "REQUEST_EXPIRED" or message startswith "BOOKING_FAILED"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

output actionGroupId string = actionGroup.id
