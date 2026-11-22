import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:music2dap_gui/main.dart';
import 'package:music2dap_gui/services/settings.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  SharedPreferences.setMockInitialValues({});

  testWidgets('app shell renders', (tester) async {
    final settings = await AppSettings.load();
    await tester.pumpWidget(Music2DapApp(settings: settings));
    expect(find.text('Music2DAP'), findsOneWidget);
    expect(find.text('Download'), findsOneWidget);
    expect(find.text('Doctor'), findsOneWidget);
  });
}
