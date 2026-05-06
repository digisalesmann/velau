import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:http/http.dart' as http;

class ApiService {
  static const String baseUrl = 'https://velau.onrender.com';
  static const Duration _timeout = Duration(seconds: 20);

  Future<http.Response> _get(Uri uri, {Map<String, String>? headers}) =>
      http.get(uri, headers: headers).timeout(_timeout);

  Future<http.Response> _post(Uri uri, {Map<String, String>? headers, Object? body}) =>
      http.post(uri, headers: headers, body: body).timeout(_timeout);

  /// Translates raw Dart/http exceptions into one-line user-friendly messages.
  static String friendlyError(dynamic e) {
    final raw = e.toString();
    if (e is TimeoutException || raw.contains('TimeoutException') || raw.contains('timed out')) {
      return 'The server took too long to respond. Please try again.';
    }
    if (e is SocketException ||
        raw.contains('SocketException') ||
        raw.contains('Failed host lookup') ||
        raw.contains('No address associated')) {
      return 'No internet connection. Please check your network and try again.';
    }
    if (raw.contains('HandshakeException') ||
        raw.contains('Connection terminated during handshake') ||
        raw.contains('CERTIFICATE_VERIFY_FAILED')) {
      return 'Secure connection failed. Please try again or check your network.';
    }
    if (raw.contains('ClientSoftware') ||
        raw.contains('connection abort') ||
        raw.contains('Connection reset') ||
        raw.contains('Connection refused')) {
      return 'Connection was interrupted. Please try again.';
    }
    if (raw.contains('FormatException') || raw.contains('Invalid data format')) {
      return 'Received an unexpected response from the server.';
    }
    // Strip "Exception: " prefix for any other server-side messages
    return raw.replaceAll('Exception: ', '');
  }

  // --- AUTHENTICATION ---
  Future<dynamic> register(String email, String password) async {
    final response = await _post(
      Uri.parse('$baseUrl/register'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'username': email, 'password': password}),
    );
    return _processResponse(response);
  }

  Future<dynamic> login(String email, String password) async {
    final response = await _post(
      Uri.parse('$baseUrl/login'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'username': email, 'password': password}),
    );
    return _processResponse(response);
  }

  Future<dynamic> loginWithFirebase(String firebaseToken) async {
    final response = await _post(
      Uri.parse('$baseUrl/auth/firebase'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({'firebase_token': firebaseToken}),
    );
    return _processResponse(response);
  }

  // --- PUSH NOTIFICATIONS ---
  Future<void> registerFcmToken(String jwtToken, String fcmToken) async {
    try {
      await _post(
        Uri.parse('$baseUrl/notifications/register'),
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer $jwtToken',
        },
        body: jsonEncode({'token': fcmToken}),
      );
    } catch (_) {}
  }

  Future<void> unregisterFcmToken(String jwtToken, String fcmToken) async {
    try {
      await _post(
        Uri.parse('$baseUrl/notifications/unregister'),
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer $jwtToken',
        },
        body: jsonEncode({'token': fcmToken}),
      );
    } catch (_) {}
  }

  // --- DASHBOARD & NEWS ---
  Future<dynamic> getDashboardData(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/dashboard'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
    );
    return _processResponse(response);
  }

  Future<dynamic> getOpenContracts(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/open_contracts'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
    );
    return _processResponse(response);
  }

  Future<dynamic> getNews(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/news'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
    );
    return _processResponse(response);
  }

  // --- CHART DATA ---
  Future<dynamic> getCandles(
    String token,
    String symbol,
    int count,
    int granularity,
  ) async {
    final response = await _post(
      Uri.parse('$baseUrl/candles'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({
        'symbol': symbol,
        'count': count,
        'granularity': granularity,
      }),
    );
    return _processResponse(response);
  }

  // --- SIGNALS ---
  Future<dynamic> getSignals(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/signals'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
    );
    return _processResponse(response);
  }

  // --- TRADING & DATA ---
  Future<dynamic> getTradeHistory(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/dashboard/history'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
    );
    return _processResponse(response);
  }

  Future<dynamic> getTicks(String token, String symbol) async {
    final response = await _post(
      Uri.parse('$baseUrl/ticks'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'symbol': symbol}),
    );
    return _processResponse(response);
  }

  Future<dynamic> placeTrade(
    String token,
    Map<String, dynamic> tradeData,
  ) async {
    final response = await _post(
      Uri.parse('$baseUrl/trade'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode(tradeData),
    );
    return _processResponse(response);
  }

  // --- BOT CONTROL ---
  Future<dynamic> getBotStatus(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/bot/status'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> toggleBot(String token) async {
    final response = await _post(
      Uri.parse('$baseUrl/bot/toggle'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  // --- COPY TRADING ---
  Future<dynamic> getMasterTraders(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/copy/traders'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
    );
    return _processResponse(response);
  }

  Future<dynamic> startCopying(String token, Map<String, dynamic> data) async {
    final response = await _post(
      Uri.parse('$baseUrl/copy/start'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode(data),
    );
    return _processResponse(response);
  }

  Future<dynamic> stopCopying(String token, int traderId) async {
    final response = await _post(
      Uri.parse('$baseUrl/copy/stop'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'trader_id': traderId}),
    );
    return _processResponse(response);
  }

  // --- ADMIN ---
  Future<dynamic> adminStats(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/admin/stats'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> adminUsers(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/admin/users'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> adminSubscriptions(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/admin/subscriptions'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> adminGrant(String token, String username, String plan) async {
    final response = await _post(
      Uri.parse('$baseUrl/admin/grant'),
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer $token'},
      body: jsonEncode({'username': username, 'plan': plan}),
    );
    return _processResponse(response);
  }

  Future<dynamic> adminRevoke(String token, int subId) async {
    final response = await _post(
      Uri.parse('$baseUrl/admin/revoke'),
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer $token'},
      body: jsonEncode({'sub_id': subId}),
    );
    return _processResponse(response);
  }

  Future<dynamic> adminSetAdmin(String token, String username, bool value) async {
    final response = await _post(
      Uri.parse('$baseUrl/admin/set_admin'),
      headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer $token'},
      body: jsonEncode({'username': username, 'value': value}),
    );
    return _processResponse(response);
  }

  // --- SUBSCRIPTION ---
  Future<dynamic> getSubscriptionStatus(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/subscription/status'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> createSubscription(String token, String plan) async {
    final response = await _post(
      Uri.parse('$baseUrl/subscription/create'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'plan': plan}),
    );
    return _processResponse(response);
  }

  Future<dynamic> pollPayment(String token, String paymentId) async {
    final response = await _get(
      Uri.parse('$baseUrl/subscription/poll/$paymentId'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> cancelPayment(String token, String paymentId) async {
    final response = await _post(
      Uri.parse('$baseUrl/subscription/cancel'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'payment_id': paymentId}),
    );
    return _processResponse(response);
  }

  // --- 2FA ---
  Future<dynamic> get2faStatus(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/2fa/status'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> setup2fa(String token) async {
    final response = await _get(
      Uri.parse('$baseUrl/2fa/setup'),
      headers: {'Authorization': 'Bearer $token'},
    );
    return _processResponse(response);
  }

  Future<dynamic> enable2fa(String token, String code) async {
    final response = await _post(
      Uri.parse('$baseUrl/2fa/enable'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'code': code}),
    );
    return _processResponse(response);
  }

  Future<dynamic> verify2fa(String token, String code) async {
    final response = await _post(
      Uri.parse('$baseUrl/2fa/verify'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'code': code}),
    );
    return _processResponse(response);
  }

  Future<dynamic> disable2fa(String token, String code) async {
    final response = await _post(
      Uri.parse('$baseUrl/2fa/disable'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $token',
      },
      body: jsonEncode({'code': code}),
    );
    return _processResponse(response);
  }

  // --- RESPONSE HANDLER ---
  dynamic _processResponse(http.Response response) {
    if (response.statusCode >= 200 && response.statusCode < 300) {
      try {
        return jsonDecode(response.body);
      } catch (_) {
        throw Exception('Invalid data format from server.');
      }
    }
    String errorMessage;
    try {
      final data = jsonDecode(response.body);
      if (data is Map) {
        errorMessage = (data['detail'] ?? data['message'] ?? response.body)
            .toString();
      } else {
        errorMessage = response.body;
      }
    } catch (_) {
      errorMessage = response.body.isNotEmpty
          ? response.body
          : 'HTTP ${response.statusCode}';
    }
    throw Exception(errorMessage);
  }

  // --- DERIV CONNECTION ---
  Future<dynamic> connectDeriv(String jwtToken, String derivToken) async {
    final response = await _post(
      Uri.parse('$baseUrl/deriv/connect'),
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer $jwtToken',
      },
      body: jsonEncode({'api_token': derivToken}),
    );
    return _processResponse(response);
  }

  Future<dynamic> disconnectDeriv(String jwtToken) async {
    final response = await _post(
      Uri.parse('$baseUrl/deriv/disconnect'),
      headers: {'Authorization': 'Bearer $jwtToken'},
    );
    return _processResponse(response);
  }

  Future<dynamic> getDerivStatus(String jwtToken) async {
    final response = await _get(
      Uri.parse('$baseUrl/deriv/status'),
      headers: {'Authorization': 'Bearer $jwtToken'},
    );
    return _processResponse(response);
  }
}
