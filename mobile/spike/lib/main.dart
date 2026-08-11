import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:serious_python/serious_python.dart';

void main() {
  runApp(const SpikeApp());
}

class SpikeApp extends StatelessWidget {
  const SpikeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'TrenchChat core spike',
      home: const SpikeScreen(),
    );
  }
}

class SpikeScreen extends StatefulWidget {
  const SpikeScreen({super.key});

  @override
  State<SpikeScreen> createState() => _SpikeScreenState();
}

class _SpikeScreenState extends State<SpikeScreen> {
  String _status = 'Not run yet';
  List<dynamic>? _steps;
  bool _running = false;

  Future<void> _runSpike() async {
    setState(() {
      _running = true;
      _status = 'Running trenchchat.core.identity / storage on-device...';
      _steps = null;
    });

    try {
      final docsDir = await getApplicationDocumentsDirectory();
      final spikeDir = Directory('${docsDir.path}/trenchchat_spike');
      await spikeDir.create(recursive: true);

      await SeriousPython.run(
        appFileName: 'app/main.py',
        sync: true,
        environmentVariables: {'TRENCHCHAT_SPIKE_DIR': spikeDir.path},
      );

      final resultFile = File('${spikeDir.path}/result.json');
      if (!await resultFile.exists()) {
        setState(() {
          _status = 'main.py ran but wrote no result.json — see device logs';
          _running = false;
        });
        return;
      }

      final parsed = jsonDecode(await resultFile.readAsString());
      setState(() {
        _steps = parsed['steps'] as List<dynamic>;
        _status = 'Done';
        _running = false;
      });
    } catch (e) {
      setState(() {
        _status = 'Crashed: $e';
        _running = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('trenchchat/core spike')),
      body: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(_status),
            const SizedBox(height: 16),
            ElevatedButton(
              onPressed: _running ? null : _runSpike,
              child: const Text('Run identity + storage round trip'),
            ),
            const SizedBox(height: 16),
            if (_steps != null)
              Expanded(
                child: ListView(
                  children: _steps!.map((s) {
                    final ok = s['ok'] == true;
                    return ListTile(
                      leading: Icon(
                        ok ? Icons.check_circle : Icons.cancel,
                        color: ok ? Colors.green : Colors.red,
                      ),
                      title: Text(s['step'] as String),
                      subtitle: Text(s['detail'] as String? ?? ''),
                    );
                  }).toList(),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
