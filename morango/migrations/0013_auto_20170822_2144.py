# -*- coding: utf-8 -*-
# Generated by Django 1.9.13 on 2017-08-22 21:44
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('morango', '0012_syncsession_profile'),
    ]

    operations = [
        migrations.AlterField(
            model_name='buffer',
            name='id',
            field=models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
        migrations.AlterUniqueTogether(
            name='buffer',
            unique_together=set([('transfer_session', 'model_uuid')]),
        ),
    ]