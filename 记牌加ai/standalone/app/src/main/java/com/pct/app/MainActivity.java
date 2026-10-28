package com.pct.app;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/** 独立记牌器：不依赖 LSPatch。frida 采集 → /sdcard/pct/ → 本 app 读卡画面板。 */
public class MainActivity extends Activity {
    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        TextView t = new TextView(this);
        t.setText("记牌加AI 独立版\n数据源: /sdcard/pct/pct_state.json\n(frida 采集器写入)");
        t.setGravity(Gravity.CENTER);
        root.addView(t);
        Button btn = new Button(this);
        btn.setText("开启记牌面板");
        btn.setOnClickListener(v -> {
            if (!Settings.canDrawOverlays(this)) {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
            } else {
                Overlay.show(getApplication());
                finish();
            }
        });
        root.addView(btn);
        setContentView(root);
    }
}
