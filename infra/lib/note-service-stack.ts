import * as path from 'node:path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as cw from 'aws-cdk-lib/aws-cloudwatch';
import * as cwActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as snsSubs from 'aws-cdk-lib/aws-sns-subscriptions';
import { NagSuppressions } from 'cdk-nag';

const APP_PORT = 8000;
const DB_NAME = 'notes';

export class NoteServiceStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ---------------------------------------------------------------- context
    const ctx = (key: string, fallback = ''): string => {
      const v = this.node.tryGetContext(key);
      return v === undefined || v === null ? fallback : String(v);
    };
    const environment = ctx('environment', 'prod');
    const dbInstanceType = ctx('dbInstanceType', 't4g.medium');
    const dbMultiAz = ctx('dbMultiAz', 'true') !== 'false';
    const corsOrigin = ctx('corsOrigin', '*'); // TODO: restrict to your web origin(s) in prod.
    const oidcIssuer = ctx('oidcIssuer');
    const oidcAudience = ctx('oidcAudience');
    const oidcJwksUrl = ctx('oidcJwksUrl');
    // Without an OIDC issuer the service can only authenticate API keys.
    const authMode = oidcIssuer ? ctx('authMode', 'both') : 'api_key';
    const certificateArn = ctx('certificateArn');
    const domainName = ctx('domainName');
    const alarmEmail = ctx('alarmEmail');
    const httpsEnabled = Boolean(certificateArn && domainName);

    // -------------------------------------------------------------------- VPC
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      // 1 NAT keeps cost down; set to 2 for AZ-independent egress (HA).
      natGateways: 1,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'app', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS, cidrMask: 22 },
        { name: 'db', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
      gatewayEndpoints: {
        S3: { service: ec2.GatewayVpcEndpointAwsService.S3 },
      },
    });

    const flowLogGroup = new logs.LogGroup(this, 'VpcFlowLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    vpc.addFlowLog('FlowLog', {
      destination: ec2.FlowLogDestination.toCloudWatchLogs(flowLogGroup),
      trafficType: ec2.FlowLogTrafficType.ALL,
    });

    // -------------------------------------------------------- security groups
    const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc,
      description: 'Public ALB ingress',
      allowAllOutbound: false,
    });
    albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'HTTP from internet');
    if (httpsEnabled) {
      albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS from internet');
    }

    const serviceSg = new ec2.SecurityGroup(this, 'ServiceSg', {
      vpc,
      description: 'note-service Fargate tasks',
      allowAllOutbound: true, // OIDC JWKS fetch, AWS APIs, ECR pulls via NAT
    });
    serviceSg.addIngressRule(albSg, ec2.Port.tcp(APP_PORT), 'App port from ALB only');
    albSg.addEgressRule(serviceSg, ec2.Port.tcp(APP_PORT), 'ALB to tasks');

    const migrateSg = new ec2.SecurityGroup(this, 'MigrateSg', {
      vpc,
      description: 'One-off migration task (no ingress)',
      allowAllOutbound: true,
    });

    const dbSg = new ec2.SecurityGroup(this, 'DbSg', {
      vpc,
      description: 'RDS PostgreSQL',
      allowAllOutbound: false,
    });
    dbSg.addIngressRule(serviceSg, ec2.Port.tcp(5432), 'Postgres from service tasks');
    dbSg.addIngressRule(migrateSg, ec2.Port.tcp(5432), 'Postgres from migration task');

    // -------------------------------------------------------------------- RDS
    const engine = rds.DatabaseInstanceEngine.postgres({ version: rds.PostgresEngineVersion.VER_17 });
    const parameterGroup = new rds.ParameterGroup(this, 'DbParams', {
      engine,
      description: 'note-service Postgres 17 (TLS enforced)',
      parameters: {
        'rds.force_ssl': '1',
        log_min_duration_statement: '1000',
      },
    });

    const db = new rds.DatabaseInstance(this, 'Database', {
      engine,
      instanceType: new ec2.InstanceType(dbInstanceType),
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [dbSg],
      credentials: rds.Credentials.fromGeneratedSecret('notes_admin'),
      databaseName: DB_NAME,
      parameterGroup,
      multiAz: dbMultiAz,
      storageType: rds.StorageType.GP3,
      allocatedStorage: 20,
      maxAllocatedStorage: 100,
      storageEncrypted: true,
      backupRetention: cdk.Duration.days(7),
      deleteAutomatedBackups: false,
      deletionProtection: true,
      removalPolicy: cdk.RemovalPolicy.SNAPSHOT,
      enablePerformanceInsights: true,
      monitoringInterval: cdk.Duration.seconds(60),
      cloudwatchLogsExports: ['postgresql'],
      cloudwatchLogsRetention: logs.RetentionDays.ONE_MONTH,
      autoMinorVersionUpgrade: true,
      iamAuthentication: true,
      publiclyAccessible: false,
      copyTagsToSnapshot: true,
    });
    const dbSecret = db.secret!;

    // ------------------------------------------------------------- S3 buckets
    const logsBucket = new s3.Bucket(this, 'AccessLogsBucket', {
      // ALB access logs only support SSE-S3.
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      lifecycleRules: [{ expiration: cdk.Duration.days(90) }],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    const attachments = new s3.Bucket(this, 'AttachmentsBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
      serverAccessLogsBucket: logsBucket,
      serverAccessLogsPrefix: 's3-attachments/',
      lifecycleRules: [
        { abortIncompleteMultipartUploadAfter: cdk.Duration.days(7) },
        { noncurrentVersionExpiration: cdk.Duration.days(90) },
      ],
      cors: [
        {
          // '*' is the default for bootstrap convenience — restrict via `-c corsOrigin=https://app.example.com`.
          allowedOrigins: [corsOrigin],
          allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET],
          allowedHeaders: ['*'],
          exposedHeaders: ['ETag'],
          maxAge: 3000,
        },
      ],
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // -------------------------------------------------------------------- ECS
    const cluster = new ecs.Cluster(this, 'Cluster', {
      vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
    });

    const imageAsset = new ecrAssets.DockerImageAsset(this, 'AppImage', {
      directory: path.join(__dirname, '..', '..'),
      platform: ecrAssets.Platform.LINUX_ARM64,
      // Repo has no .dockerignore; keep the build context lean and never ship infra/, venvs or VCS data.
      exclude: [
        'infra',
        '.venv',
        'venv',
        '.git',
        '.github',
        'tests',
        '.pytest_cache',
        '**/__pycache__',
        '**/*.pyc',
        '.env',
        '.env.*',
      ],
    });
    const image = ecs.ContainerImage.fromDockerImageAsset(imageAsset);

    const appLogGroup = new logs.LogGroup(this, 'AppLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const dbEnv: Record<string, string> = {
      DB_HOST: db.dbInstanceEndpointAddress,
      DB_PORT: db.dbInstanceEndpointPort,
      DB_NAME,
      DB_SSLMODE: 'require',
      LOG_LEVEL: 'INFO',
      LOG_FORMAT: 'json',
      AWS_REGION: this.region,
    };
    const dbSecrets: Record<string, ecs.Secret> = {
      DB_USER: ecs.Secret.fromSecretsManager(dbSecret, 'username'),
      DB_PASSWORD: ecs.Secret.fromSecretsManager(dbSecret, 'password'),
    };
    const runtimePlatform: ecs.RuntimePlatform = {
      cpuArchitecture: ecs.CpuArchitecture.ARM64,
      operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
    };

    // ---- app task
    const taskDef = new ecs.FargateTaskDefinition(this, 'AppTask', {
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform,
    });
    taskDef.addVolume({ name: 'tmp' }); // ephemeral scratch so the root FS can be read-only

    const container = taskDef.addContainer('app', {
      image,
      essential: true,
      readonlyRootFilesystem: true,
      portMappings: [{ containerPort: APP_PORT, protocol: ecs.Protocol.TCP }],
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'app', logGroup: appLogGroup }),
      environment: {
        ...dbEnv,
        AUTH_MODE: authMode,
        OIDC_ISSUER: oidcIssuer,
        OIDC_AUDIENCE: oidcAudience,
        OIDC_JWKS_URL: oidcJwksUrl,
        ATTACHMENTS_BUCKET: attachments.bucketName,
        RATE_LIMIT_PER_MINUTE: '120',
        RUN_MIGRATIONS_ON_STARTUP: 'false',
      },
      secrets: dbSecrets,
      healthCheck: {
        // Liveness only (no DB) — readiness is the ALB's /readyz check.
        command: [
          'CMD',
          'python',
          '-c',
          "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 3,
        startPeriod: cdk.Duration.seconds(20),
      },
    });
    container.addMountPoints({ containerPath: '/tmp', sourceVolume: 'tmp', readOnly: false });
    attachments.grantReadWrite(taskDef.taskRole);

    // Init container: every task applies pending migrations before the app starts. Safe to run
    // from many tasks at once (advisory lock; each file applied exactly once), so the very first
    // deploy and every schema change need no manual step. The one-off task below stays available
    // for running migrations ahead of a rollout.
    const initMigrate = taskDef.addContainer('migrate', {
      image,
      essential: false,
      readonlyRootFilesystem: true,
      command: ['python', '-m', 'app.migrate'],
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'init-migrate', logGroup: appLogGroup }),
      environment: dbEnv,
      secrets: dbSecrets,
    });
    initMigrate.addMountPoints({ containerPath: '/tmp', sourceVolume: 'tmp', readOnly: false });
    container.addContainerDependencies({ container: initMigrate, condition: ecs.ContainerDependencyCondition.SUCCESS });

    // ---- one-off migration task (same image, command override)
    const migrateTaskDef = new ecs.FargateTaskDefinition(this, 'MigrateTask', {
      cpu: 256,
      memoryLimitMiB: 512,
      runtimePlatform,
    });
    migrateTaskDef.addVolume({ name: 'tmp' });
    const migrateContainer = migrateTaskDef.addContainer('migrate', {
      image,
      essential: true,
      readonlyRootFilesystem: true,
      command: ['python', '-m', 'app.migrate'],
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'migrate', logGroup: appLogGroup }),
      environment: dbEnv,
      secrets: dbSecrets,
    });
    migrateContainer.addMountPoints({ containerPath: '/tmp', sourceVolume: 'tmp', readOnly: false });

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 2,
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      securityGroups: [serviceSg],
      circuitBreaker: { enable: true, rollback: true },
      enableExecuteCommand: false,
      healthCheckGracePeriod: cdk.Duration.seconds(60),
      propagateTags: ecs.PropagatedTagSource.SERVICE,
    });

    // -------------------------------------------------------------------- ALB
    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      dropInvalidHeaderFields: true,
      idleTimeout: cdk.Duration.seconds(60),
    });
    alb.logAccessLogs(logsBucket, 'alb');

    const targetGroup = new elbv2.ApplicationTargetGroup(this, 'AppTargets', {
      vpc,
      port: APP_PORT,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      targets: [service],
      deregistrationDelay: cdk.Duration.seconds(30),
      healthCheck: {
        path: '/readyz', // readiness: includes DB connectivity
        healthyHttpCodes: '200',
        interval: cdk.Duration.seconds(15),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
      },
    });

    let appListener: elbv2.ApplicationListener;
    if (httpsEnabled) {
      appListener = alb.addListener('Https', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [acm.Certificate.fromCertificateArn(this, 'Cert', certificateArn)],
        sslPolicy: elbv2.SslPolicy.RECOMMENDED_TLS,
        defaultTargetGroups: [targetGroup],
      });
      alb.addRedirect({
        sourcePort: 80,
        sourceProtocol: elbv2.ApplicationProtocol.HTTP,
        targetPort: 443,
        targetProtocol: elbv2.ApplicationProtocol.HTTPS,
      });
    } else {
      // WARNING: plain HTTP. Bearer tokens / API keys travel in cleartext.
      // Pass -c certificateArn=... -c domainName=... before real use.
      appListener = alb.addListener('Http', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultTargetGroups: [targetGroup],
      });
    }

    // Prometheus metrics are internal-only: never serve /metrics through the public ALB.
    appListener.addAction('BlockMetrics', {
      priority: 1,
      conditions: [elbv2.ListenerCondition.pathPatterns(['/metrics', '/metrics/*'])],
      action: elbv2.ListenerAction.fixedResponse(404, {
        contentType: 'application/json',
        messageBody: '{"detail":"Not Found"}',
      }),
    });

    // ------------------------------------------------------------ autoscaling
    const scaling = service.autoScaleTaskCount({ minCapacity: 2, maxCapacity: 6 });
    scaling.scaleOnCpuUtilization('Cpu', {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.seconds(120),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });
    scaling.scaleOnRequestCount('Requests', {
      requestsPerTarget: 1000, // per target per minute
      targetGroup,
      scaleInCooldown: cdk.Duration.seconds(120),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // -------------------------------------------------------------------- WAF
    const managedRule = (name: string, priority: number): wafv2.CfnWebACL.RuleProperty => ({
      name,
      priority,
      overrideAction: { none: {} },
      statement: { managedRuleGroupStatement: { vendorName: 'AWS', name } },
      visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true, metricName: name },
    });
    const webAcl = new wafv2.CfnWebACL(this, 'WebAcl', {
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true, metricName: 'NoteServiceWebAcl' },
      rules: [
        {
          // Cross-instance rate limit per client IP (the app also limits per credential in-process).
          name: 'RateLimitPerIp',
          priority: 0,
          action: { block: {} },
          statement: { rateBasedStatement: { limit: 1000, evaluationWindowSec: 300, aggregateKeyType: 'IP' } },
          visibilityConfig: { cloudWatchMetricsEnabled: true, sampledRequestsEnabled: true, metricName: 'RateLimitPerIp' },
        },
        managedRule('AWSManagedRulesCommonRuleSet', 1),
        managedRule('AWSManagedRulesKnownBadInputsRuleSet', 2),
        managedRule('AWSManagedRulesSQLiRuleSet', 3),
      ],
    });
    new wafv2.CfnWebACLAssociation(this, 'WebAclAssociation', {
      resourceArn: alb.loadBalancerArn,
      webAclArn: webAcl.attrArn,
    });

    // ----------------------------------------------------------------- alarms
    let alarmTopic: sns.Topic | undefined;
    if (alarmEmail) {
      alarmTopic = new sns.Topic(this, 'AlarmTopic', { displayName: 'note-service alarms', enforceSSL: true });
      alarmTopic.addSubscription(new snsSubs.EmailSubscription(alarmEmail));
    }
    const wire = (alarm: cw.Alarm) => {
      if (alarmTopic) {
        alarm.addAlarmAction(new cwActions.SnsAction(alarmTopic));
        alarm.addOkAction(new cwActions.SnsAction(alarmTopic));
      }
      return alarm;
    };

    wire(
      new cw.MathExpression({
        expression: 'FILL(elb5xx, 0) + FILL(tgt5xx, 0)',
        usingMetrics: {
          elb5xx: alb.metrics.httpCodeElb(elbv2.HttpCodeElb.ELB_5XX_COUNT, { period: cdk.Duration.minutes(5) }),
          tgt5xx: targetGroup.metrics.httpCodeTarget(elbv2.HttpCodeTarget.TARGET_5XX_COUNT, {
            period: cdk.Duration.minutes(5),
          }),
        },
        label: 'ALB + target 5xx',
        period: cdk.Duration.minutes(5),
      }).createAlarm(this, 'Alb5xxAlarm', {
        alarmDescription: 'ALB/target 5xx responses >= 10 in 5 min',
        threshold: 10,
        evaluationPeriods: 1,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        treatMissingData: cw.TreatMissingData.NOT_BREACHING,
      }),
    );
    wire(
      targetGroup.metrics
        .targetResponseTime({ statistic: 'p95', period: cdk.Duration.minutes(5) })
        .createAlarm(this, 'LatencyP95Alarm', {
          alarmDescription: 'Target response time p95 > 1s for 15 min',
          threshold: 1,
          evaluationPeriods: 3,
          comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
          treatMissingData: cw.TreatMissingData.NOT_BREACHING,
        }),
    );
    wire(
      db.metricCPUUtilization({ period: cdk.Duration.minutes(5) }).createAlarm(this, 'DbCpuAlarm', {
        alarmDescription: 'RDS CPU > 80% for 15 min',
        threshold: 80,
        evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
      }),
    );
    wire(
      db.metricFreeStorageSpace({ period: cdk.Duration.minutes(5) }).createAlarm(this, 'DbFreeStorageAlarm', {
        alarmDescription: 'RDS free storage < 5 GiB',
        threshold: 5 * 1024 ** 3,
        evaluationPeriods: 1,
        comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD,
      }),
    );
    wire(
      new cw.Metric({
        namespace: 'ECS/ContainerInsights',
        metricName: 'RunningTaskCount',
        dimensionsMap: { ClusterName: cluster.clusterName, ServiceName: service.serviceName },
        statistic: 'Minimum',
        period: cdk.Duration.minutes(1),
      }).createAlarm(this, 'RunningTasksAlarm', {
        alarmDescription: 'ECS running task count < 2',
        threshold: 2,
        evaluationPeriods: 3,
        comparisonOperator: cw.ComparisonOperator.LESS_THAN_THRESHOLD,
        treatMissingData: cw.TreatMissingData.BREACHING,
      }),
    );

    // ---------------------------------------------------------------- outputs
    const scheme = httpsEnabled ? 'https' : 'http';
    const host = httpsEnabled ? domainName : alb.loadBalancerDnsName;
    new cdk.CfnOutput(this, 'ServiceUrl', { value: `${scheme}://${host}` });
    new cdk.CfnOutput(this, 'LoadBalancerDns', {
      value: alb.loadBalancerDnsName,
      description: httpsEnabled ? `Point ${domainName} (CNAME/alias) here` : 'Public ALB DNS name',
    });
    if (!httpsEnabled) {
      new cdk.CfnOutput(this, 'HttpsWarning', {
        value:
          'ALB is HTTP-only: credentials are sent in cleartext. Redeploy with -c certificateArn=<acm-arn> -c domainName=<fqdn>.',
      });
      cdk.Annotations.of(this).addWarningV2(
        'note-service:http-only',
        'No certificateArn/domainName context: ALB listens on HTTP only.',
      );
    }
    new cdk.CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'ServiceName', { value: service.serviceName });
    new cdk.CfnOutput(this, 'AttachmentsBucketName', { value: attachments.bucketName });
    new cdk.CfnOutput(this, 'DbEndpoint', { value: db.dbInstanceEndpointAddress });
    new cdk.CfnOutput(this, 'DbSecretArn', { value: dbSecret.secretArn });
    new cdk.CfnOutput(this, 'AppLogGroupName', { value: appLogGroup.logGroupName });
    new cdk.CfnOutput(this, 'MigrationTaskDefinitionArn', { value: migrateTaskDef.taskDefinitionArn });

    const privateSubnetIds = vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }).subnetIds;
    new cdk.CfnOutput(this, 'MigrationRunTaskCommand', {
      description: 'Run DB migrations (one-off) before rolling out new app code',
      value: cdk.Fn.join('', [
        'aws ecs run-task --region ',
        this.region,
        ' --cluster ',
        cluster.clusterName,
        ' --task-definition ',
        migrateTaskDef.taskDefinitionArn,
        ' --launch-type FARGATE --count 1',
        ' --network-configuration "awsvpcConfiguration={subnets=[',
        cdk.Fn.join(',', privateSubnetIds),
        '],securityGroups=[',
        migrateSg.securityGroupId,
        '],assignPublicIp=DISABLED}"',
        " --query 'tasks[0].taskArn' --output text",
      ]),
    });

    // --------------------------------------------------------- cdk-nag notes
    NagSuppressions.addResourceSuppressions(albSg, [
      { id: 'AwsSolutions-EC23', reason: 'Internet-facing ALB must accept 80/443 from anywhere; WAF fronts it.' },
    ]);
    NagSuppressions.addResourceSuppressions(logsBucket, [
      { id: 'AwsSolutions-S1', reason: 'This is the access-log destination bucket; logging it to itself would loop.' },
    ]);
    NagSuppressions.addResourceSuppressions(
      db,
      [
        {
          id: 'AwsSolutions-SMG4',
          reason:
            'Tasks read DB creds at start-up; automatic rotation would break running tasks until a redeploy. Rotate manually during a deploy window.',
        },
        { id: 'AwsSolutions-RDS11', reason: 'Default port 5432 is fine; DB is in isolated subnets and SG-restricted.' },
      ],
      true,
    );
    if (!dbMultiAz) {
      NagSuppressions.addResourceSuppressions(db, [
        { id: 'AwsSolutions-RDS3', reason: 'Multi-AZ explicitly disabled via -c dbMultiAz=false (non-prod/cost).' },
      ]);
    }
    NagSuppressions.addResourceSuppressions(
      [taskDef, migrateTaskDef],
      [
        {
          id: 'AwsSolutions-ECS2',
          reason: 'Only non-secret configuration is passed as plain env vars; DB credentials come from Secrets Manager.',
        },
      ],
    );
    NagSuppressions.addResourceSuppressions(
      [taskDef.obtainExecutionRole(), migrateTaskDef.obtainExecutionRole()],
      [
        {
          id: 'AwsSolutions-IAM5',
          reason: 'ecr:GetAuthorizationToken does not support resource-level permissions (CDK-generated).',
          appliesTo: ['Resource::*'],
        },
      ],
      true,
    );
    NagSuppressions.addResourceSuppressionsByPath(
      this,
      `/${this.stackName}/Database/MonitoringRole/Resource`,
      [
        {
          id: 'AwsSolutions-IAM4',
          reason: 'AmazonRDSEnhancedMonitoringRole is the AWS-documented policy for RDS Enhanced Monitoring.',
          appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole'],
        },
      ],
    );
    // CDK's singleton LogRetention custom resource (sets the RDS postgresql log group retention).
    const logRetentionFn = this.node.children.find((c) => c.node.id.startsWith('LogRetention'));
    if (logRetentionFn) {
      NagSuppressions.addResourceSuppressions(
        logRetentionFn,
        [
          {
            id: 'AwsSolutions-IAM4',
            reason: 'CDK-managed LogRetention helper uses AWSLambdaBasicExecutionRole for its own logs.',
            appliesTo: ['Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'],
          },
          {
            id: 'AwsSolutions-IAM5',
            reason: 'CDK-managed LogRetention helper needs logs:PutRetentionPolicy on log groups it does not own yet.',
            appliesTo: ['Resource::*'],
          },
        ],
        true,
      );
    }
    NagSuppressions.addResourceSuppressions(
      taskDef,
      [
        {
          id: 'AwsSolutions-IAM5',
          reason: 'grantReadWrite scopes object actions to <attachments-bucket>/*; required for per-object keys.',
          appliesTo: [
            'Action::s3:Abort*',
            'Action::s3:DeleteObject*',
            'Action::s3:GetBucket*',
            'Action::s3:GetObject*',
            'Action::s3:List*',
            { regex: '/^Resource::<AttachmentsBucket.*\\.Arn>/\\*$/g' },
          ],
        },
      ],
      true,
    );
  }
}
