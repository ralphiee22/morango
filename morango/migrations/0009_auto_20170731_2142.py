# -*- coding: utf-8 -*-
# Generated by Django 1.9.13 on 2017-07-31 21:42
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('morango', '0008_auto_20170731_0228'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='syncsession',
            name='host',
        ),
        migrations.RemoveField(
            model_name='syncsession',
            name='local_scope',
        ),
        migrations.RemoveField(
            model_name='syncsession',
            name='remote_scope',
        ),
        migrations.AddField(
            model_name='syncsession',
            name='connection_kind',
            field=models.CharField(choices=[('network', 'Network'), ('disk', 'Disk')], default='', max_length=10),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='syncsession',
            name='connection_params',
            field=models.TextField(default='{}'),
        ),
        migrations.AddField(
            model_name='syncsession',
            name='connection_path',
            field=models.CharField(default='', max_length=1000),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='syncsession',
            name='local_certificate',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='syncsessions_local', to='morango.Certificate'),
        ),
        migrations.AddField(
            model_name='syncsession',
            name='remote_certificate',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='syncsessions_remote', to='morango.Certificate'),
        ),
    ]