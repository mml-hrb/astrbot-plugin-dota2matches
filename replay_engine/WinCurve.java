/*
 * 录像解析引擎的一个导出器：把 demo 里的「天辉胜率曲线」读出来。
 *
 * 编译（在引擎目录下，即含 classes/ 与 lib/ 的那一层）：
 *
 *     javac -proc:none --release 21 -encoding UTF-8 \
 *           -cp "classes:lib/*" -d classes WinCurve.java
 *
 *     --release 21 不能省：目标机的 JRE 往往就是 21，
 *     用更高版本的 JDK 编出来会 UnsupportedClassVersionError。
 *     -proc:none 关掉 Clarity 自带的注解处理器（否则每次编译都打一堆警告）。
 *
 * 编完的 WinCurve.class 必须放进引擎的 classes/。缺了它不会报错 ——
 * 战报只是静默少掉「胜负走势」与「阵容先验」两段，所以部署后要单独核对一次。
 */

import skadistats.clarity.model.Entity;
import skadistats.clarity.model.FieldPath;
import skadistats.clarity.processor.entities.OnEntityCreated;
import skadistats.clarity.processor.entities.OnEntityPropertyChanged;
import skadistats.clarity.processor.runner.Context;
import skadistats.clarity.processor.runner.SimpleRunner;
import skadistats.clarity.source.MappedFileSource;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 只导出「天辉胜率曲线」，不碰战斗日志、不采样血量。
 *
 * <p>为什么单独一个导出器：战报要判「哪一波才是关键团」，而阵亡人数和
 * 经济增量都判不出来。Valve 自己在观战界面算的那条胜率曲线就写在 demo 里，
 * 读出来整局只要六秒多，比完整解析（五到七分钟）便宜两个数量级。
 *
 * <p>两处数据源，互为兜底：
 * <ul>
 *   <li>{@code CDOTA_DataSpectator.m_fRadiantWinProbability} —— 0~1 浮点，
 *       逐次更新，实测整局八十来次、中位间隔十五秒，精度最高；</li>
 *   <li>{@code CDOTASpectatorGraphManagerProxy} 上的 64 点整数数组 ——
 *       每隔约 19.6 秒一个点，覆盖整局，序列太短时用它。</li>
 * </ul>
 *
 * <p>输出的时间一律是 <b>demo 时间</b>（含选人阶段）。换算成比赛时间要减去
 * {@code end - duration}，由 Python 侧现算 —— 偏移每局都不一样，不能写死。
 */
public class WinCurve {

    /** 64 点数组可能挂在两种路径下，读的时候逐个试，不硬编码一种。 */
    private static final String[] GRAPH_PREFIXES = {
            "m_pGraphManager.m_rgRadiantWinChance.",
            "m_rgRadiantWinChance.",
    };

    private static final String[] GRAPH_START = {
            "m_pGraphManager.m_flTotalEarnedGoldStartTime",
            "m_flTotalEarnedGoldStartTime",
    };

    private static final String[] GRAPH_END = {
            "m_pGraphManager.m_flTotalEarnedGoldEndTime",
            "m_flTotalEarnedGoldEndTime",
    };

    private final List<double[]> series = new ArrayList<>();

    private Entity graphEntity;

    @OnEntityCreated(classPattern = "CDOTASpectatorGraphManagerProxy")
    public void onGraph(Context ctx, Entity e) {
        graphEntity = e;
    }

    @OnEntityPropertyChanged(
            classPattern = "CDOTA_DataSpectator",
            propertyPattern = "m_fRadiantWinProbability")
    public void onProb(Context ctx, Entity e, FieldPath fp) {
        Object raw = e.getPropertyForFieldPath(fp);
        if (raw == null) {
            return;
        }
        double value;
        try {
            value = Double.parseDouble(String.valueOf(raw));
        } catch (NumberFormatException ex) {
            return;
        }
        // ⚠️ series 的量纲必须是 0~1 —— Python 侧会乘 100 再显示。
        // 这里若先乘成 0~100，战报上会出现「5400% 胜率」这种表面能跑、
        // 数值全错的输出，比直接报错更危险。
        if (value > 1.0001 && value <= 100.0) {
            value /= 100.0;
        }
        if (value < 0.0 || value > 1.0) {
            return;
        }
        double t = ctx.getTick() * (double) ctx.getMillisPerTick() / 1000.0;
        // 同一次更新里可能触发多遍（属性路径分段监听），只保留最后一条。
        if (!series.isEmpty()) {
            double[] last = series.get(series.size() - 1);
            if (Math.abs(last[0] - t) < 0.5) {
                last[1] = value;
                return;
            }
        }
        series.add(new double[]{t, value});
    }

    private static Object read(Entity e, String name) {
        try {
            if (!e.hasProperty(name)) {
                return null;
            }
            FieldPath fp = e.getFieldPathForName(name);
            if (fp == null) {
                return null;
            }
            return e.getPropertyForFieldPath(fp);
        } catch (RuntimeException ex) {
            return null;
        }
    }

    private static Double readNumber(Entity e, String name) {
        Object raw = read(e, name);
        if (raw == null) {
            return null;
        }
        try {
            return Double.parseDouble(String.valueOf(raw));
        } catch (NumberFormatException ex) {
            return null;
        }
    }

    private double[] readBucket() {
        if (graphEntity == null) {
            return null;
        }
        String prefix = null;
        for (String p : GRAPH_PREFIXES) {
            if (read(graphEntity, String.format(Locale.ROOT, "%s%04d", p, 0)) != null) {
                prefix = p;
                break;
            }
        }
        if (prefix == null) {
            return null;
        }
        double[] bucket = new double[64];
        int got = 0;
        for (int i = 0; i < 64; i++) {
            Double v = readNumber(graphEntity, String.format(Locale.ROOT, "%s%04d", prefix, i));
            if (v == null) {
                bucket[i] = -1.0;
                continue;
            }
            // 这一组是整数量纲（0~100），偶尔带 -1 表示「还没到这一段」。
            bucket[i] = (v >= 0.0 && v <= 1.0001) ? v * 100.0 : v;
            if (bucket[i] >= 0.0) {
                got++;
            }
        }
        return got >= 20 ? bucket : null;
    }

    private static Double firstNumber(Entity e, String[] names) {
        if (e == null) {
            return null;
        }
        for (String n : names) {
            Double v = readNumber(e, n);
            if (v != null && v > 0.0) {
                return v;
            }
        }
        return null;
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("usage: WinCurve <demo>");
            System.exit(2);
        }
        WinCurve handler = new WinCurve();
        new SimpleRunner(new MappedFileSource(new File(args[0]))).runWith(handler);
        System.out.println("WINCHANCE:" + handler.toJson());
    }

    private String toJson() {
        double[] bucket = readBucket();
        double start = 0.0;
        double end = 0.0;
        Double s = firstNumber(graphEntity, GRAPH_START);
        Double e = firstNumber(graphEntity, GRAPH_END);
        if (s != null) {
            start = s;
        }
        if (e != null) {
            end = e;
        }
        // 数组读不到时用逐次采样的首尾兜底，保证 Python 侧总能算出偏移。
        if (series.isEmpty() && bucket == null) {
            return "{\"start\":0,\"end\":0,\"chance\":[],\"series\":[]}";
        }
        if (end <= 0.0 && !series.isEmpty()) {
            end = series.get(series.size() - 1)[0];
        }
        if (start <= 0.0 && !series.isEmpty()) {
            start = series.get(0)[0];
        }

        StringBuilder sb = new StringBuilder(8192);
        sb.append("{\"start\":").append(fmt(start));
        sb.append(",\"end\":").append(fmt(end));

        sb.append(",\"chance\":[");
        if (bucket != null) {
            for (int i = 0; i < bucket.length; i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(bucket[i] < 0 ? "-1" : String.valueOf(Math.round(bucket[i])));
            }
        }
        sb.append("],\"radiant\":[");
        if (bucket != null) {
            for (int i = 0; i < bucket.length; i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(bucket[i] < 0 ? "-1" : String.valueOf(Math.round(bucket[i])));
            }
        }
        sb.append("],\"dire\":[");
        if (bucket != null) {
            for (int i = 0; i < bucket.length; i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(bucket[i] < 0 ? "-1" : String.valueOf(Math.round(100.0 - bucket[i])));
            }
        }
        sb.append("],\"series\":[");
        for (int i = 0; i < series.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append('[').append(fmt(series.get(i)[0])).append(',')
              .append(fmt(series.get(i)[1])).append(']');
        }
        sb.append("]}");
        return sb.toString();
    }

    private static String fmt(double v) {
        return String.format(Locale.ROOT, "%.2f", v);
    }
}
