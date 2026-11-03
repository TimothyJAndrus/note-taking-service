#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
import { NoteServiceStack } from '../lib/note-service-stack';

const app = new cdk.App();

const environment = (app.node.tryGetContext('environment') as string | undefined) || 'prod';

new NoteServiceStack(app, 'NoteServiceStack', {
  description: 'note-taking-service: ECS Fargate (arm64) + ALB/WAF + RDS PostgreSQL 17 + S3 attachments',
  env: {
    // Account may be undefined for env-agnostic synth; region defaults to us-west-2.
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || 'us-west-2',
  },
  terminationProtection: environment === 'prod',
});

cdk.Tags.of(app).add('Project', 'note-taking-service');
cdk.Tags.of(app).add('Environment', environment);

// cdk-nag: AWS Solutions rule pack. Unsuppressed errors fail `cdk synth`.
cdk.Aspects.of(app).add(new AwsSolutionsChecks({ verbose: true }));
