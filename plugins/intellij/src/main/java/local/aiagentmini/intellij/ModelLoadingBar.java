package local.aiagentmini.intellij;

import javax.swing.*;
import java.awt.*;

/** Indeterminate model-load activity; completion colours fade without claiming a percentage. */
final class ModelLoadingBar extends JPanel {
    private static final Color BLUE = new Color(55, 155, 255);
    private static final Color GREEN = new Color(64, 202, 125);
    private static final Color RED = new Color(242, 87, 98);
    private final Timer animation;
    private boolean loading;
    private boolean disposed;
    private long began;
    private long completed;
    private Color colour = BLUE;

    ModelLoadingBar(int height) {
        setOpaque(false);
        setPreferredSize(new Dimension(0, height));
        setMinimumSize(new Dimension(0, height));
        setVisible(false);
        getAccessibleContext().setAccessibleName("Model loading status");
        animation = new Timer(40, event -> {
            if (!loading && (System.nanoTime() - completed) / 1_000_000L >= 1500) {
                ((Timer) event.getSource()).stop();
                setVisible(false);
                if (getParent() != null) getParent().revalidate();
            } else repaint();
        });
        animation.setCoalesce(true);
    }

    boolean isLoading() { return loading; }

    void begin() {
        if (disposed || loading) return;
        loading = true;
        colour = BLUE;
        began = System.nanoTime();
        setStatus("Loading model…");
        setVisible(true);
        animation.start();
        if (getParent() != null) getParent().revalidate();
        repaint();
    }

    void finish(boolean success) {
        if (disposed || !loading) return; // Polls must not restart the completion fade.
        loading = false;
        colour = success ? GREEN : RED;
        completed = System.nanoTime();
        setStatus(success ? "Model loaded successfully" : "Model loading failed or was cancelled");
        repaint();
    }

    private void setStatus(String status) {
        setToolTipText(status);
        getAccessibleContext().setAccessibleDescription(status);
    }

    @Override protected void paintComponent(Graphics graphics) {
        super.paintComponent(graphics);
        Graphics2D g = (Graphics2D) graphics.create();
        try {
            g.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
            float opacity = loading ? 1f : Math.max(0f, 1f - Math.max(0f,
                    (System.nanoTime() - completed) / 1_000_000f - 450f) / 1050f);
            int y = getHeight() / 2;
            for (int glow = 3; glow >= 0; glow--) {
                g.setComposite(AlphaComposite.SrcOver.derive(opacity * (glow == 0 ? .85f : .08f)));
                g.setColor(colour);
                g.setStroke(new BasicStroke(Math.max(1f, getHeight() * (.2f + glow * .2f)),
                        BasicStroke.CAP_ROUND, BasicStroke.JOIN_ROUND));
                g.drawLine(0, y, getWidth(), y);
            }
            if (loading && getWidth() > 0) {
                float position = (float) ((System.nanoTime() - began) / 1_000_000_000.0 % 1.8 / 1.8);
                float span = Math.max(25f, getWidth() * .35f);
                float left = position * (getWidth() + span * 2) - span * 2;
                g.setComposite(AlphaComposite.SrcOver);
                g.setPaint(new LinearGradientPaint(left, 0, left + span * 2, 0,
                        new float[]{0f, .5f, 1f}, new Color[]{new Color(55, 155, 255, 0),
                        new Color(170, 222, 255), new Color(55, 155, 255, 0)}));
                g.setStroke(new BasicStroke(Math.max(2f, getHeight() * .45f), BasicStroke.CAP_ROUND,
                        BasicStroke.JOIN_ROUND));
                g.drawLine(0, y, getWidth(), y);
            }
        } finally { g.dispose(); }
    }

    void dispose() {
        disposed = true;
        animation.stop();
        setVisible(false);
    }
}
