import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../services/api_service.dart';

class AdminScreen extends StatefulWidget {
  const AdminScreen({super.key});

  @override
  State<AdminScreen> createState() => _AdminScreenState();
}

class _AdminScreenState extends State<AdminScreen>
    with SingleTickerProviderStateMixin {
  final ApiService _api = ApiService();

  static const Color _bg        = Color(0xFFFFFFFF);
  static const Color _textDark  = Color(0xFF0F172A);
  static const Color _textMuted = Color(0xFF64748B);
  static const Color _green     = Color(0xFF10B981);
  static const Color _red       = Color(0xFFEF4444);
  static const Color _separator = Color(0xFFF1F5F9);
  static const Color _border    = Color(0xFFE2E8F0);

  late TabController _tabs;

  // Stats
  Map<String, dynamic>? _stats;

  // Users
  List<dynamic> _users = [];

  // Subscriptions
  List<dynamic> _subs = [];

  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 3, vsync: this);
    _load();
  }

  @override
  void dispose() {
    _tabs.dispose();
    super.dispose();
  }

  Future<String?> _token() async {
    final prefs = await SharedPreferences.getInstance();
    return prefs.getString('jwt_token');
  }

  Future<void> _load() async {
    setState(() { _loading = true; _error = null; });
    try {
      final t = await _token();
      if (t == null) throw Exception('Not authenticated.');
      final results = await Future.wait([
        _api.adminStats(t),
        _api.adminUsers(t),
        _api.adminSubscriptions(t),
      ]);
      if (mounted) {
        setState(() {
          _stats  = results[0] as Map<String, dynamic>;
          _users  = (results[1] as Map)['users'] as List;
          _subs   = (results[2] as Map)['subscriptions'] as List;
          _loading = false;
        });
      }
    } catch (e) {
      if (mounted) setState(() { _error = e.toString().replaceAll('Exception: ', ''); _loading = false; });
    }
  }

  TextStyle _satoshi({
    required Color color,
    required double fontSize,
    required FontWeight fontWeight,
    double letterSpacing = 0,
  }) => TextStyle(
    fontFamily: 'Satoshi',
    color: color,
    fontSize: fontSize,
    fontWeight: fontWeight,
    letterSpacing: letterSpacing,
  );

  // ── Grant bottom sheet ───────────────────────────────────────────────────────
  void _showGrantDialog() {
    String selectedUser = _users.isNotEmpty ? (_users[0]['username'] ?? '') : '';
    String selectedPlan = 'monthly';

    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      backgroundColor: Colors.transparent,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setSheet) => Container(
          decoration: const BoxDecoration(
            color: _bg,
            borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
          ),
          padding: EdgeInsets.fromLTRB(
            24, 20, 24,
            24 + MediaQuery.of(ctx).viewInsets.bottom,
          ),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Handle
              Center(
                child: Container(
                  width: 36, height: 4,
                  decoration: BoxDecoration(
                    color: _border,
                    borderRadius: BorderRadius.circular(2),
                  ),
                ),
              ),
              const SizedBox(height: 20),
              Text('Grant Subscription',
                  style: _satoshi(color: _textDark, fontSize: 20, fontWeight: FontWeight.w900, letterSpacing: -0.4)),
              const SizedBox(height: 20),

              // User picker
              Text('User', style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: 0.4)),
              const SizedBox(height: 6),
              DropdownButtonFormField<String>(
                initialValue: selectedUser,
                decoration: InputDecoration(
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                    borderSide: const BorderSide(color: _border),
                  ),
                  enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(12),
                    borderSide: const BorderSide(color: _border),
                  ),
                  contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                ),
                items: _users.map<DropdownMenuItem<String>>((u) {
                  final name = u['username']?.toString() ?? '';
                  return DropdownMenuItem(value: name,
                      child: Text(name, style: _satoshi(color: _textDark, fontSize: 13, fontWeight: FontWeight.w500)));
                }).toList(),
                onChanged: (v) => setSheet(() => selectedUser = v ?? ''),
              ),
              const SizedBox(height: 16),

              // Plan picker
              Text('Plan', style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: 0.4)),
              const SizedBox(height: 6),
              Row(children: [
                _planChip('Monthly',  'monthly',  selectedPlan, (v) => setSheet(() => selectedPlan = v)),
                const SizedBox(width: 8),
                _planChip('Yearly',   'yearly',   selectedPlan, (v) => setSheet(() => selectedPlan = v)),
                const SizedBox(width: 8),
                _planChip('Lifetime', 'lifetime', selectedPlan, (v) => setSheet(() => selectedPlan = v)),
              ]),
              const SizedBox(height: 28),

              // Grant button
              SizedBox(
                width: double.infinity,
                child: ElevatedButton(
                  style: ElevatedButton.styleFrom(
                    backgroundColor: _textDark,
                    elevation: 0,
                    padding: const EdgeInsets.symmetric(vertical: 16),
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(100)),
                  ),
                  onPressed: () async {
                    Navigator.pop(ctx);
                    try {
                      final t = await _token();
                      if (t == null) return;
                      await _api.adminGrant(t, selectedUser, selectedPlan);
                      _snack('Subscription granted.', color: _green);
                      _load();
                    } catch (e) {
                      _snack(e.toString().replaceAll('Exception: ', ''), color: _red);
                    }
                  },
                  child: Text('Grant Access',
                      style: _satoshi(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w700)),
                ),
              ),
          ],
        ),
      ),
    ),
    );
  }

  Widget _planChip(String label, String value, String selected, ValueChanged<String> onSelect) {
    final active = value == selected;
    return Expanded(
      child: GestureDetector(
        onTap: () => onSelect(value),
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 12),
          decoration: BoxDecoration(
            color: active ? _textDark : _separator,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: active ? _textDark : _border),
          ),
          child: Center(
            child: Text(
              label,
              style: _satoshi(
                color: active ? Colors.white : _textMuted,
                fontSize: 13,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ),
      ),
    );
  }

  void _snack(String msg, {Color color = const Color(0xFF0F172A)}) {
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(msg, style: _satoshi(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w500)),
      backgroundColor: color,
      behavior: SnackBarBehavior.floating,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
    ));
  }

  // ── Build ────────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: _bg,
      appBar: AppBar(
        backgroundColor: _bg,
        elevation: 0,
        scrolledUnderElevation: 0,
        centerTitle: false,
        titleSpacing: 24,
        title: Text('Admin', style: _satoshi(color: _textDark, fontSize: 24, fontWeight: FontWeight.w900, letterSpacing: -0.5)),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh_rounded),
            color: _textDark,
            onPressed: _load,
          ),
          IconButton(
            icon: const Icon(Icons.redeem_rounded),
            color: _textDark,
            tooltip: 'Grant subscription',
            onPressed: _users.isEmpty ? null : _showGrantDialog,
          ),
          const SizedBox(width: 8),
        ],
        bottom: TabBar(
          controller: _tabs,
          labelColor: _textDark,
          unselectedLabelColor: _textMuted,
          indicatorColor: _green,
          indicatorWeight: 2,
          labelStyle: _satoshi(color: _textDark, fontSize: 13, fontWeight: FontWeight.w700),
          unselectedLabelStyle: _satoshi(color: _textMuted, fontSize: 13, fontWeight: FontWeight.w500),
          tabs: const [
            Tab(text: 'Overview'),
            Tab(text: 'Users'),
            Tab(text: 'Subscriptions'),
          ],
        ),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator(color: _green, strokeWidth: 2))
          : _error != null
          ? _buildError()
          : TabBarView(
              controller: _tabs,
              children: [
                _buildOverview(),
                _buildUsers(),
                _buildSubscriptions(),
              ],
            ),
    );
  }

  // ── Overview tab ─────────────────────────────────────────────────────────────
  Widget _buildOverview() {
    final stats    = _stats ?? {};
    final byPlan   = (stats['by_plan'] as Map<String, dynamic>?) ?? {};
    final monthly  = byPlan['monthly']  ?? 0;
    final yearly   = byPlan['yearly']   ?? 0;
    final lifetime = byPlan['lifetime'] ?? 0;

    return RefreshIndicator(
      color: _green,
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          // Stat cards
          Row(children: [
            _statCard('Total Users',      '${stats['total_users'] ?? 0}', Icons.people_rounded),
            const SizedBox(width: 12),
            _statCard('Active Subs',      '${stats['active_subs'] ?? 0}',  Icons.verified_rounded, color: _green),
          ]),
          const SizedBox(height: 12),
          _statCard(
            'Total Revenue',
            '\$${(stats['total_revenue'] ?? 0.0).toStringAsFixed(2)}',
            Icons.attach_money_rounded,
            color: _green,
            wide: true,
          ),
          const SizedBox(height: 24),

          // Plan breakdown
          Text('Active by Plan',
              style: _satoshi(color: _textMuted, fontSize: 11, fontWeight: FontWeight.w700, letterSpacing: 0.6)),
          const SizedBox(height: 12),
          _planRow('Monthly',  monthly,  '\$149.90/mo'),
          _divider(),
          _planRow('Yearly',   yearly,   '\$1,699/yr'),
          _divider(),
          _planRow('Lifetime', lifetime, '\$7,800'),
        ],
      ),
    );
  }

  Widget _statCard(String label, String value, IconData icon, {Color? color, bool wide = false}) {
    final card = Container(
      width: wide ? double.infinity : null,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: _separator,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 20, color: color ?? _textMuted),
          const SizedBox(height: 8),
          Text(value, style: _satoshi(color: _textDark, fontSize: 24, fontWeight: FontWeight.w900, letterSpacing: -0.5)),
          const SizedBox(height: 2),
          Text(label, style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w500)),
        ],
      ),
    );
    // Wide cards sit directly in a ListView — no Expanded needed.
    // Narrow cards sit inside a Row — wrap in Expanded to share space equally.
    return wide ? card : Expanded(child: card);
  }

  Widget _planRow(String plan, dynamic count, String price) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Row(
        children: [
          Expanded(child: Text(plan, style: _satoshi(color: _textDark, fontSize: 14, fontWeight: FontWeight.w700))),
          Text('$count active', style: _satoshi(color: _textMuted, fontSize: 13, fontWeight: FontWeight.w500)),
          const SizedBox(width: 16),
          Text(price, style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w500)),
        ],
      ),
    );
  }

  Widget _divider() => Divider(color: _border, height: 1, thickness: 1);

  // ── Users tab ────────────────────────────────────────────────────────────────
  Widget _buildUsers() {
    if (_users.isEmpty) {
      return Center(child: Text('No users yet.', style: _satoshi(color: _textMuted, fontSize: 15, fontWeight: FontWeight.w500)));
    }
    return RefreshIndicator(
      color: _green,
      onRefresh: _load,
      child: ListView.separated(
        padding: const EdgeInsets.symmetric(vertical: 8),
        itemCount: _users.length,
        separatorBuilder: (context, index) => Divider(color: _border, height: 1, indent: 24, endIndent: 24),
        itemBuilder: (_, i) {
          final u       = _users[i];
          final name    = u['username']?.toString() ?? '—';
          final plan    = u['plan']?.toString();
          final status  = u['sub_status']?.toString();
          final isAdmin = u['is_admin'] == true || u['is_admin'] == 1;
          final active  = status == 'active';

          return ListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 4),
            title: Row(children: [
              Text(name, style: _satoshi(color: _textDark, fontSize: 14, fontWeight: FontWeight.w700)),
              if (isAdmin) ...[
                const SizedBox(width: 6),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(color: _textDark, borderRadius: BorderRadius.circular(4)),
                  child: Text('ADMIN', style: _satoshi(color: Colors.white, fontSize: 9, fontWeight: FontWeight.w900, letterSpacing: 0.4)),
                ),
              ],
            ]),
            subtitle: Text(
              plan != null ? '$plan · ${status ?? ''}' : 'No subscription',
              style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w400),
            ),
            trailing: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              decoration: BoxDecoration(
                color: active ? _green.withValues(alpha: 0.1) : _separator,
                borderRadius: BorderRadius.circular(6),
              ),
              child: Text(
                active ? 'Active' : status ?? 'None',
                style: _satoshi(
                  color: active ? _green : _textMuted,
                  fontSize: 11,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
          );
        },
      ),
    );
  }

  // ── Subscriptions tab ─────────────────────────────────────────────────────────
  Widget _buildSubscriptions() {
    if (_subs.isEmpty) {
      return Center(child: Text('No subscriptions yet.', style: _satoshi(color: _textMuted, fontSize: 15, fontWeight: FontWeight.w500)));
    }
    return RefreshIndicator(
      color: _green,
      onRefresh: _load,
      child: ListView.separated(
        padding: const EdgeInsets.symmetric(vertical: 8),
        itemCount: _subs.length,
        separatorBuilder: (context, index) => Divider(color: _border, height: 1, indent: 24, endIndent: 24),
        itemBuilder: (_, i) {
          final s       = _subs[i];
          final subId   = s['id'] as int? ?? 0;
          final user    = s['username']?.toString() ?? '—';
          final plan    = s['plan']?.toString() ?? '—';
          final status  = s['status']?.toString() ?? '—';
          final usd     = (s['price_usd'] as num?)?.toStringAsFixed(2) ?? '0.00';
          final active  = status == 'active';
          final manual  = (s['payment_id']?.toString() ?? '').startsWith('manual_');

          return ListTile(
            contentPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 6),
            title: Row(children: [
              Flexible(
                child: Text(user, overflow: TextOverflow.ellipsis, style: _satoshi(color: _textDark, fontSize: 14, fontWeight: FontWeight.w700)),
              ),
              const SizedBox(width: 8),
              Text(_capitalize(plan), style: _satoshi(color: _textMuted, fontSize: 13, fontWeight: FontWeight.w500)),
              if (manual) ...[
                const SizedBox(width: 6),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 2),
                  decoration: BoxDecoration(color: _separator, borderRadius: BorderRadius.circular(4)),
                  child: Text('MANUAL', style: _satoshi(color: _textMuted, fontSize: 9, fontWeight: FontWeight.w900, letterSpacing: 0.3)),
                ),
              ],
            ]),
            subtitle: Text(
              manual ? 'Manual grant' : '\$$usd ${s['pay_currency'] ?? ''}',
              style: _satoshi(color: _textMuted, fontSize: 12, fontWeight: FontWeight.w400),
            ),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                  decoration: BoxDecoration(
                    color: active ? _green.withValues(alpha: 0.1) : _separator,
                    borderRadius: BorderRadius.circular(6),
                  ),
                  child: Text(
                    _capitalize(status),
                    style: _satoshi(
                      color: active ? _green : _textMuted,
                      fontSize: 11,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                if (active) ...[
                  const SizedBox(width: 8),
                  GestureDetector(
                    onTap: () => _confirmRevoke(subId, user),
                    child: Container(
                      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                      decoration: BoxDecoration(
                        color: _red.withValues(alpha: 0.08),
                        borderRadius: BorderRadius.circular(6),
                        border: Border.all(color: _red.withValues(alpha: 0.2)),
                      ),
                      child: Text('Revoke', style: _satoshi(color: _red, fontSize: 11, fontWeight: FontWeight.w700)),
                    ),
                  ),
                ],
              ],
            ),
          );
        },
      ),
    );
  }

  void _confirmRevoke(int subId, String username) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: _bg,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        title: Text('Revoke subscription?',
            style: _satoshi(color: _textDark, fontSize: 17, fontWeight: FontWeight.w900)),
        content: Text('This will immediately revoke $username\'s access.',
            style: _satoshi(color: _textMuted, fontSize: 14, fontWeight: FontWeight.w400)),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text('Cancel', style: _satoshi(color: _textMuted, fontSize: 14, fontWeight: FontWeight.w600)),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: _red, elevation: 0,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(100)),
            ),
            onPressed: () async {
              Navigator.pop(ctx);
              try {
                final t = await _token();
                if (t == null) return;
                await _api.adminRevoke(t, subId);
                _snack('Subscription revoked.', color: _red);
                _load();
              } catch (e) {
                _snack(e.toString().replaceAll('Exception: ', ''), color: _red);
              }
            },
            child: Text('Revoke', style: _satoshi(color: Colors.white, fontSize: 14, fontWeight: FontWeight.w700)),
          ),
        ],
      ),
    );
  }

  Widget _buildError() => Center(
    child: Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.error_outline_rounded, color: _red, size: 48),
          const SizedBox(height: 16),
          Text(_error!, textAlign: TextAlign.center,
              style: _satoshi(color: _textMuted, fontSize: 14, fontWeight: FontWeight.w500)),
          const SizedBox(height: 24),
          GestureDetector(
            onTap: _load,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
              decoration: BoxDecoration(color: _textDark, borderRadius: BorderRadius.circular(100)),
              child: Text('Retry', style: _satoshi(color: Colors.white, fontSize: 14, fontWeight: FontWeight.w700)),
            ),
          ),
        ],
      ),
    ),
  );

  String _capitalize(String s) => s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);
}
