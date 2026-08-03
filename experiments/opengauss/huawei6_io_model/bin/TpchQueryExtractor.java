import com.oltpbenchmark.WorkloadConfiguration;
import com.oltpbenchmark.api.Procedure;
import com.oltpbenchmark.api.StatementDialects;
import com.oltpbenchmark.benchmarks.tpch.procedures.GenericQuery;
import com.oltpbenchmark.types.DatabaseType;
import com.oltpbenchmark.util.RandomGenerator;

import java.lang.reflect.Method;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.util.Random;

public final class TpchQueryExtractor {
    private TpchQueryExtractor() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 5) {
            System.err.println("usage: TpchQueryExtractor <url> <user> <password> <query_id> <scale_factor>");
            System.exit(2);
        }

        Class.forName("org.postgresql.Driver");
        int queryId = Integer.parseInt(args[3]);
        double scaleFactor = Double.parseDouble(args[4]);
        Random random = new Random();
        random.setSeed(15721L);
        RandomGenerator generator = new RandomGenerator(random.nextInt());

        GenericQuery query = newQuery(queryId);
        initializeQuery(query);
        try (Connection conn = DriverManager.getConnection(args[0], args[1], args[2]);
             PreparedStatement statement = getStatement(query, conn, generator, scaleFactor)) {
            System.out.println(statement.toString());
        }
    }

    private static GenericQuery newQuery(int queryId) throws Exception {
        String className = "com.oltpbenchmark.benchmarks.tpch.procedures.Q" + queryId;
        Class<?> cls = Class.forName(className);
        return (GenericQuery) cls.getDeclaredConstructor().newInstance();
    }

    private static void initializeQuery(GenericQuery query) throws Exception {
        WorkloadConfiguration config = new WorkloadConfiguration();
        config.setBenchmarkName("tpch");
        config.setDatabaseType(DatabaseType.POSTGRES);
        StatementDialects dialects = new StatementDialects(config);

        Method initialize = Procedure.class.getDeclaredMethod("initialize", DatabaseType.class);
        initialize.setAccessible(true);
        initialize.invoke(query, DatabaseType.POSTGRES);

        Method loadSQLDialect = Procedure.class.getDeclaredMethod("loadSQLDialect", StatementDialects.class);
        loadSQLDialect.setAccessible(true);
        loadSQLDialect.invoke(query, dialects);
    }

    private static PreparedStatement getStatement(
            GenericQuery query,
            Connection connection,
            RandomGenerator generator,
            double scaleFactor) throws Exception {
        Method method = query.getClass().getDeclaredMethod(
                "getStatement", Connection.class, RandomGenerator.class, double.class);
        method.setAccessible(true);
        return (PreparedStatement) method.invoke(query, connection, generator, scaleFactor);
    }
}
